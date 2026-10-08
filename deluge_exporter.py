#!/usr/bin/env python3
"""Export Deluge torrent and session metrics as InfluxDB line protocol.

Polls the Deluge Web UI's JSON-RPC API and writes to InfluxDB 1.x (`/write`),
InfluxDB 2.x (`/api/v2/write` with a token) or VictoriaMetrics (`/write`).
Standard library only. Configuration is by environment variable; see README.md.
"""
import argparse
import base64
import http.cookiejar
import json
import logging
import os
import signal
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

__version__ = "1.0.0"

REQUEST_TIMEOUT_SECONDS = 10
SUMMARY_EVERY_SECONDS = 3600
ALERT_AFTER_FAILURES = 10
DEFAULT_PASSWORD = "deluge"

TORRENT_PARAMS = ["name", "state", "progress", "num_seeds", "total_seeds", "num_peers", "total_peers",
                  "download_payload_rate", "upload_payload_rate", "ratio", "tracker_host", "total_done",
                  "total_uploaded", "label",
                  "seeding_time", "active_time", "distributed_copies", "total_size", "total_wanted",
                  "total_remaining", "time_added", "completed_time", "time_since_upload",
                  "last_seen_complete", "eta", "private", "tracker_status", "message"]
TAG_PARAMS = ("tracker_host", "name", "label")
# Text read only to derive tracker_ok and message_ok; never written as-is.
TEXT_PARAMS = ("tracker_status", "message")

# libtorrent session counters (core.get_session_status), written on `stats`
# under these field names. Keys a Deluge build does not know are left out.
SESSION_KEYS = {
    "net.recv_bytes": "recv_bytes",
    "net.sent_bytes": "sent_bytes",
    "net.recv_failed_bytes": "recv_failed_bytes",
    "peer.num_peers_connected": "peers_connected",
    "peer.num_peers_half_open": "peers_half_open",
    "peer.banned_for_hash_failure": "peers_banned_for_hash_failure",
    "peer.num_banned_peers": "peers_banned",
    "disk.queued_write_bytes": "disk_queued_write_bytes",
}

# Fields that are always written as floats, even when Deluge sends a whole
# number: InfluxDB fixes a field's type on first write and rejects a change.
FLOAT_FIELDS = {
    "torrent": {"progress", "ratio", "distributed_copies"},
    "stats": {"download_rate", "upload_rate", "download_protocol_rate", "upload_protocol_rate",
              "max_download", "max_upload"},
}

# Measurements whose fields are all known: every numeric field outside
# FLOAT_FIELDS is written as an integer even if Deluge sends 3.0.
INT_MEASUREMENTS = {"torrent", "blocklist"}

# Numeric twin of the `state` text, for stores that cannot hold strings.
# Order and names are deluge.common.TORRENT_STATE (Deluge 2.x), numbered from 1.
STATE_CODES = {"Allocating": 1, "Checking": 2, "Downloading": 3, "Seeding": 4,
               "Paused": 5, "Error": 6, "Queued": 7, "Moving": 8}
UNKNOWN_STATE_CODE = -1

# The public IP address: no value as a metric, and text that VictoriaMetrics cannot store.
DROPPED_STATS = ("external_ip",)

log = logging.getLogger("deluge-exporter")


# --- configuration -----------------------------------------------------------

class ConfigError(Exception):
    pass


class Config:
    """Settings read from environment variables. Required: DELUGE_URL, INFLUXDB_URL."""

    def __init__(self, env):
        url = _required(env, "DELUGE_URL").rstrip("/")
        self.deluge_url = url if url.endswith("/json") else url + "/json"
        self.deluge_password = env.get("DELUGE_PASSWORD") or DEFAULT_PASSWORD
        self.deluge_verify_ssl = _env_bool(env, "DELUGE_VERIFY_SSL", True)
        self.influxdb_url = _required(env, "INFLUXDB_URL").rstrip("/")
        self.influxdb_database = env.get("INFLUXDB_DATABASE") or "deluge"
        self.influxdb_username = env.get("INFLUXDB_USERNAME", "")
        self.influxdb_password = env.get("INFLUXDB_PASSWORD", "")
        # InfluxDB 2.x: a token switches to the v2 write API
        self.influxdb_token = env.get("INFLUXDB_TOKEN", "")
        self.influxdb_org = env.get("INFLUXDB_ORG", "")
        self.influxdb_bucket = env.get("INFLUXDB_BUCKET") or self.influxdb_database
        if self.influxdb_token and not self.influxdb_org:
            raise ConfigError("INFLUXDB_ORG is required when INFLUXDB_TOKEN is set")
        self.interval = _env_int(env, "INTERVAL", 30)
        self.year_tag = _env_bool(env, "YEAR_TAG", False)


def _required(env, name):
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError("{0} is required".format(name))
    return value


def _env_bool(env, name, default):
    value = env.get(name, "").strip().lower()
    if value == "":
        return default
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError("{0} must be true or false, got {1!r}".format(name, value))


def _env_int(env, name, default):
    value = env.get(name, "").strip()
    if value == "":
        return default
    try:
        number = int(value)
    except ValueError:
        raise ConfigError("{0} must be a whole number of seconds, got {1!r}".format(name, value))
    if number < 1:
        raise ConfigError("{0} must be at least 1, got {1}".format(name, number))
    return number


# --- clients -----------------------------------------------------------------

class DelugeError(Exception):
    pass


class InfluxError(Exception):
    pass


class DelugeClient:
    """Deluge Web UI JSON-RPC. Each fetch logs in, reads, and logs out, so no
    session is left open between polls."""

    def __init__(self, url, password, verify_ssl=True):
        self.url = url
        self.password = password
        self.context = None if verify_ssl else ssl._create_unverified_context()
        self.blocklist_enabled = True   # set False once Deluge says the plugin is not loaded
        self.optional_failing = set()   # optional calls currently failing, to log each once

    def fetch(self):
        """Return {"ui": web.update_ui result, "session": {...} or None,
        "blocklist": {...} or None}. Torrents and stats are required; the
        session counters and the Blocklist plugin are best effort."""
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=self.context))
        if self._rpc(opener, "auth.login", [self.password], 1) is not True:
            raise DelugeError("auth.login rejected the password")
        try:
            ui = self._rpc(opener, "web.update_ui", [TORRENT_PARAMS, {}], 2)
            session = self._optional(opener, "core.get_session_status", [list(SESSION_KEYS)])
            blocklist = None
            if self.blocklist_enabled:
                blocklist = self._optional(opener, "blocklist.get_status", [])
            return {"ui": ui, "session": session, "blocklist": blocklist}
        finally:
            try:
                self._rpc(opener, "auth.delete_session", [], 3)
            except Exception as e:  # logging out is best effort
                log.debug("auth.delete_session failed: %s", one_line(e))

    def _optional(self, opener, method, params):
        try:
            result = self._rpc(opener, method, params, 4)
        except DelugeError as e:
            if method.startswith("blocklist.") and "Unknown method" in str(e):
                self.blocklist_enabled = False
                log.info("Blocklist plugin is not enabled in Deluge; blocklist metrics are skipped until restart")
                return None
            if method not in self.optional_failing:
                self.optional_failing.add(method)
                log.warning("%s failed, its metrics are skipped: %s", method, one_line(e))
            return None
        if method in self.optional_failing:
            self.optional_failing.discard(method)
            log.info("%s working again", method)
        return result

    def _rpc(self, opener, method, params, request_id):
        body = json.dumps({"id": request_id, "method": method, "params": params}).encode()
        request = urllib.request.Request(self.url, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            answer = json.load(response)
        error = answer.get("error")
        if error:
            raise DelugeError("{0}: {1}".format(method, error.get("message", error) if isinstance(error, dict) else error))
        return answer.get("result")


class InfluxWriter:
    """InfluxDB 1.x / VictoriaMetrics `/write`, or with a token InfluxDB 2.x `/api/v2/write`."""

    def __init__(self, url, database, username="", password="", token="", org="", bucket=""):
        self.auth = None
        if token:
            query = urllib.parse.urlencode({"org": org, "bucket": bucket or database, "precision": "ms"})
            self.url = "{0}/api/v2/write?{1}".format(url, query)
            self.auth = "Token " + token
            return
        self.url = "{0}/write?{1}".format(url, urllib.parse.urlencode({"db": database, "precision": "ms"}))
        if username:
            token = "{0}:{1}".format(username, password).encode()
            self.auth = "Basic " + base64.b64encode(token).decode()

    def write(self, lines):
        if not lines:
            return
        request = urllib.request.Request(self.url, data="\n".join(lines).encode(), method="POST",
                                         headers={"Content-Type": "text/plain; charset=utf-8"})
        if self.auth:
            request.add_header("Authorization", self.auth)
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS):
                pass
        except urllib.error.HTTPError as e:
            detail = e.read(300).decode("utf-8", "replace").strip()
            raise InfluxError("HTTP {0}: {1}".format(e.code, detail))


# --- line protocol -----------------------------------------------------------

def _escape(value, chars):
    text = str(value).replace("\n", " ")
    for c in chars:
        text = text.replace(c, "\\" + c)
    return text


def _field_value(value, as_float=False, as_int=False):
    if isinstance(value, bool):
        return "{0}i".format(int(value))
    if isinstance(value, (int, float)) and as_float:
        value = float(value)
    elif isinstance(value, float) and as_int and value == value and abs(value) != float("inf"):
        value = int(value)
    if isinstance(value, int):
        return "{0}i".format(value)
    if isinstance(value, float):
        return repr(value) if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, str):
        return '"{0}"'.format(value.replace("\\", "\\\\").replace('"', '\\"'))
    return None


def to_line(measurement, tags, fields, timestamp_ms):
    """One InfluxDB line, or None when no field remains. Tags with an empty
    value are left out (line protocol cannot carry them); None fields too."""
    floats = FLOAT_FIELDS.get(measurement, ())
    ints = measurement in INT_MEASUREMENTS
    rendered = []
    for key in sorted(fields):
        if fields[key] is None:
            continue
        text = _field_value(fields[key], key in floats, ints and key not in floats)
        if text is not None:
            rendered.append("{0}={1}".format(_escape(key, ", ="), text))
    if not rendered:
        return None
    head = _escape(measurement, ", ")
    for key in sorted(tags):
        value = "" if tags[key] is None else str(tags[key])
        if value:
            head += ",{0}={1}".format(_escape(key, ", ="), _escape(value, ", ="))
    return "{0} {1} {2}".format(head, ",".join(rendered), timestamp_ms)


# --- points ------------------------------------------------------------------

class StateCoder:
    """Maps a torrent state to its code; logs each unknown state once."""

    def __init__(self):
        self.unknown_seen = set()

    def __call__(self, state):
        if state in STATE_CODES:
            return STATE_CODES[state]
        if state not in self.unknown_seen:
            self.unknown_seen.add(state)
            log.warning("unknown torrent state %r written as state_code=%d", state, UNKNOWN_STATE_CODE)
        return UNKNOWN_STATE_CODE


def tracker_ok(status):
    """0 when the tracker reports an error or warning (e.g. 'Error: unregistered
    torrent'), otherwise 1, including 'Announce OK' and 'Announce Sent'."""
    return 0 if str(status or "").strip().lower().startswith(("error", "warning")) else 1


def message_ok(message):
    """0 when Deluge has an error message for the torrent (missing files, …)."""
    return 1 if str(message or "").strip() in ("", "OK") else 0


def build_points(data, timestamp_ms, year_tag, state_code):
    """(measurement, tags, fields) for every torrent, one stats point and, when
    the Blocklist plugin answered, one blocklist point."""
    common = {"year": time.strftime("%Y", time.gmtime(timestamp_ms / 1000))} if year_tag else {}
    result = data.get("ui") or {}
    points = []
    torrents = result.get("torrents") or {}
    for torrent_hash, params in torrents.items():
        fields = {k: v for k, v in params.items()
                  if k not in TAG_PARAMS and k not in TEXT_PARAMS and v is not None}
        fields["state_code"] = state_code(params.get("state"))
        if "tracker_status" in params:
            fields["tracker_ok"] = tracker_ok(params["tracker_status"])
        if "message" in params:
            fields["message_ok"] = message_ok(params["message"])
        tags = dict(common, hex=torrent_hash)
        tags.update({k: params.get(k) or "" for k in TAG_PARAMS})
        points.append(("torrent", tags, fields))
    stats = {k: v for k, v in (result.get("stats") or {}).items() if k not in DROPPED_STATS and v is not None}
    stats["torrents_count"] = len(torrents)
    for key, name in SESSION_KEYS.items():
        value = (data.get("session") or {}).get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            stats[name] = int(value)
    points.append(("stats", dict(common), stats))
    blocklist = data.get("blocklist")
    if blocklist:
        points.append(("blocklist", dict(common), {
            "num_blocked": blocklist.get("num_blocked"),
            "num_whited": blocklist.get("num_whited"),
            "up_to_date": blocklist.get("up_to_date"),
            "file_date": int(blocklist["file_date"]) if blocklist.get("file_date") else None,
        }))
    return points


# --- the exporter ------------------------------------------------------------

def one_line(error, limit=300):
    """Exception as 'Type: message' on one line, bounded."""
    text = " ".join(str(error).split())
    if len(text) > limit:
        text = text[:limit] + "..."
    return "{0}: {1}".format(type(error).__name__, text)


class Exporter:
    def __init__(self, config, deluge, writer):
        self.config = config
        self.deluge = deluge
        self.writer = writer
        self.state_code = StateCoder()

    def run_cycle(self):
        timestamp_ms = int(time.time() * 1000)
        points = build_points(self.deluge.fetch(), timestamp_ms, self.config.year_tag, self.state_code)
        lines = [to_line(m, t, f, timestamp_ms) for m, t, f in points]
        self.writer.write([line for line in lines if line])
        return sum(1 for p in points if p[0] == "torrent")

    def run(self, once=False):
        cycles = ok = failed = consecutive = torrents = 0
        failing_since = None
        summary_due = time.monotonic() + SUMMARY_EVERY_SECONDS
        while True:
            started = time.monotonic()
            cycles += 1
            try:
                torrents = self.run_cycle()
                ok += 1
                if consecutive:
                    log.info("recovered after %d failed cycle(s) since %s", consecutive, failing_since)
                consecutive, failing_since = 0, None
                log.debug("wrote %d torrents", torrents)
            except Exception as e:  # an outage or a bug: log it, keep the loop alive
                failed += 1
                consecutive += 1
                failing_since = failing_since or time.strftime("%Y-%m-%dT%H:%M:%S")
                log.warning("cycle failed: %s (consecutive=%d)", one_line(e), consecutive)
                if consecutive == ALERT_AFTER_FAILURES:
                    log.error("%d consecutive failed cycles since %s; no data is being written",
                              consecutive, failing_since)
            if once:
                return 0 if consecutive == 0 else 1
            if time.monotonic() >= summary_due:
                log.info("summary: cycles=%d ok=%d failed=%d torrents=%d", cycles, ok, failed, torrents)
                cycles = ok = failed = 0
                summary_due = time.monotonic() + SUMMARY_EVERY_SECONDS
            time.sleep(max(0.0, self.config.interval - (time.monotonic() - started)))


def main(argv=None, env=None):
    parser = argparse.ArgumentParser(description="Export Deluge metrics to InfluxDB line protocol.")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit (1 on failure)")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"),
                        help="DEBUG adds one line per cycle (env LOG_LEVEL)")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s",
                        level=getattr(logging, args.log_level.upper(), logging.INFO))
    try:
        config = Config(os.environ if env is None else env)
    except ConfigError as e:
        log.error("configuration: %s", e)
        return 2

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    target = ("bucket={0} org={1} (InfluxDB 2 API)".format(config.influxdb_bucket, config.influxdb_org)
              if config.influxdb_token else "db={0}".format(config.influxdb_database))
    log.info("starting %s: deluge=%s influxdb=%s %s interval=%ds year_tag=%s",
             __version__, config.deluge_url, config.influxdb_url, target, config.interval, config.year_tag)
    if config.deluge_password == DEFAULT_PASSWORD:
        log.warning("DELUGE_PASSWORD is Deluge's default; anyone who can reach the Web UI can log in with it")
    exporter = Exporter(config,
                        DelugeClient(config.deluge_url, config.deluge_password, config.deluge_verify_ssl),
                        InfluxWriter(config.influxdb_url, config.influxdb_database,
                                     config.influxdb_username, config.influxdb_password,
                                     config.influxdb_token, config.influxdb_org, config.influxdb_bucket))
    return exporter.run(once=args.once)


if __name__ == "__main__":
    sys.exit(main())
