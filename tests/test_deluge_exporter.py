import contextlib
import http.server
import io
import json
import os
import tempfile
import time
import sys
import threading
import unittest
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import deluge_exporter as de  # noqa: E402

_HEARTBEAT_DIR = tempfile.TemporaryDirectory()


def setUpModule():
    # Every test that runs the exporter would otherwise stamp the real /tmp heartbeat.
    de.HEARTBEAT_FILE = os.path.join(_HEARTBEAT_DIR.name, "heartbeat")


def tearDownModule():
    _HEARTBEAT_DIR.cleanup()

TS = 1791500000123  # 2026-10-08 UTC


def torrent(**overrides):
    """A torrent shaped like web.update_ui output."""
    t = {"name": "Some Linux ISO", "state": "Seeding", "progress": 100.0, "num_seeds": 0,
         "total_seeds": 12, "num_peers": 1, "total_peers": 40, "download_payload_rate": 0,
         "upload_payload_rate": 2048, "ratio": 1.5, "tracker_host": "example.org",
         "total_done": 734003200, "total_uploaded": 1101004800, "label": "linux",
         "seeding_time": 720000, "active_time": 730000, "distributed_copies": 3.25,
         "total_size": 734003200, "total_wanted": 734003200, "total_remaining": 0,
         "time_added": 1785735602, "completed_time": 1785740000, "time_since_upload": 120,
         "last_seen_complete": 1791490000, "eta": 0, "private": True,
         "tracker_status": "Announce OK", "message": "OK"}
    t.update(overrides)
    return t


STATS = {"upload_rate": 2100.5, "download_rate": 0.0, "upload_protocol_rate": 300.0,
         "download_protocol_rate": 120.0, "num_connections": 41, "dht_nodes": 260,
         "free_space": 5000000000000, "has_incoming_connections": 1, "max_download": -1.0,
         "max_upload": -1.0, "max_num_connections": 200, "external_ip": "203.0.113.7"}


SESSION = {"net.recv_bytes": 91921079362, "net.sent_bytes": 15428668130, "net.recv_failed_bytes": 2621440,
           "peer.num_peers_connected": 2, "peer.num_peers_half_open": 17,
           "peer.banned_for_hash_failure": 5, "peer.num_banned_peers": 25, "disk.queued_write_bytes": 0}

BLOCKLIST = {"state": "Idle", "up_to_date": False, "num_whited": 0, "num_blocked": 2938, "file_progress": 0.0,
             "file_url": "https://example.org/list.gz", "file_size": 8491012, "file_date": 1790083456.0,
             "file_type": "SafePeer (GZip)", "whitelisted": []}


def ui(torrents=None, stats=None):
    return {"torrents": {"aa11": torrent()} if torrents is None else torrents,
            "stats": dict(STATS) if stats is None else stats, "connected": True}


def result(torrents=None, stats=None, session=SESSION, blocklist=BLOCKLIST):
    return {"ui": ui(torrents, stats), "session": session, "blocklist": blocklist}


def fields_of(line):
    _, fields, _ = line.rsplit(" ", 2)
    out = {}
    for part in fields.split(","):
        k, v = part.split("=", 1)
        out[k] = v
    return out


class LineProtocolTests(unittest.TestCase):
    def test_types(self):
        line = de.to_line("m", {}, {"i": 3, "f": 1.5, "b": True, "s": 'Se"ed\\ing', "n": None}, 7)
        self.assertEqual(line, r'm b=1i,f=1.5,i=3i,s="Se\"ed\\ing" 7')

    def test_empty_tags_left_out(self):
        line = de.to_line("torrent", {"label": "", "name": "A B,C=D", "x": None}, {"v": 1}, 1)
        self.assertEqual(line, r"torrent,name=A\ B\,C\=D v=1i 1")

    def test_float_fields_stay_float_when_deluge_sends_a_whole_number(self):
        self.assertEqual(fields_of(de.to_line("torrent", {}, {"progress": 100, "ratio": 0, "num_seeds": 3}, 1)),
                         {"num_seeds": "3i", "progress": "100.0", "ratio": "0.0"})
        self.assertEqual(fields_of(de.to_line("stats", {}, {"download_rate": 0, "dht_nodes": 5}, 1)),
                         {"dht_nodes": "5i", "download_rate": "0.0"})

    def test_no_fields_gives_no_line(self):
        self.assertIsNone(de.to_line("m", {"t": "x"}, {"v": None}, 1))

    def test_torrent_fields_forced_to_integer(self):
        self.assertEqual(fields_of(de.to_line("torrent", {}, {"eta": 3.0, "num_seeds": 2.7, "progress": 50}, 1)),
                         {"eta": "3i", "num_seeds": "2i", "progress": "50.0"})

    def test_stats_floats_from_deluge_kept(self):
        self.assertEqual(fields_of(de.to_line("stats", {}, {"something_new": 2.5}, 1)), {"something_new": "2.5"})


class PointTests(unittest.TestCase):
    def test_torrent_and_stats(self):
        points = de.build_points(result(), TS, False, de.StateCoder())
        (m, tags, fields), (sm, stags, sfields), _ = points
        self.assertEqual(m, "torrent")
        self.assertEqual(tags, {"hex": "aa11", "name": "Some Linux ISO", "label": "linux", "tracker_host": "example.org"})
        self.assertEqual(fields["state"], "Seeding")
        self.assertEqual(fields["state_code"], 4)
        self.assertNotIn("name", fields)
        for text in ("tracker_status", "message"):
            self.assertNotIn(text, fields)
        self.assertEqual((fields["tracker_ok"], fields["message_ok"]), (1, 1))
        self.assertEqual((fields["seeding_time"], fields["distributed_copies"], fields["private"]), (720000, 3.25, True))
        self.assertEqual((sm, stags), ("stats", {}))
        self.assertNotIn("external_ip", sfields)
        self.assertEqual(sfields["torrents_count"], 1)
        self.assertEqual(sfields["recv_bytes"], 91921079362)
        self.assertEqual(sfields["peers_banned_for_hash_failure"], 5)
        self.assertEqual(sfields["disk_queued_write_bytes"], 0)

    def test_blocklist_point(self):
        points = de.build_points(result(), TS, False, de.StateCoder())
        self.assertEqual(points[2], ("blocklist", {}, {"num_blocked": 2938, "num_whited": 0,
                                                        "up_to_date": False, "file_date": 1790083456}))
        self.assertEqual(de.to_line(*points[2], 1), "blocklist file_date=1790083456i,num_blocked=2938i,"
                                                    "num_whited=0i,up_to_date=0i 1")

    def test_optional_data_missing(self):
        points = de.build_points(result(session=None, blocklist=None), TS, False, de.StateCoder())
        self.assertEqual([p[0] for p in points], ["torrent", "stats"])
        self.assertNotIn("recv_bytes", points[1][2])

    def test_tracker_and_message_ok(self):
        for status, ok in (("Announce OK", 1), ("Announce Sent", 1), ("", 1), (None, 1),
                           ("Error: unregistered torrent", 0), ("Warning: tracker overloaded", 0)):
            self.assertEqual(de.tracker_ok(status), ok, status)
        for message, ok in (("OK", 1), ("", 1), (None, 1), ("Missing or invalid torrent data!", 0)):
            self.assertEqual(de.message_ok(message), ok, message)

    def test_year_tag(self):
        points = de.build_points(result(), TS, True, de.StateCoder())
        self.assertEqual(points[0][1]["year"], "2026")
        self.assertEqual(points[1][1], {"year": "2026"})
        self.assertEqual(points[2][1], {"year": "2026"})

    def test_year_is_utc(self):
        new_year_utc = 1798761600000  # 2027-01-01T00:00:00Z, still 2026 in the Americas
        self.assertEqual(de.build_points(result(), new_year_utc, True, de.StateCoder())[0][1]["year"], "2027")

    def test_missing_label_and_none_values(self):
        points = de.build_points(result({"bb22": torrent(label=None, ratio=None)}), TS, False, de.StateCoder())
        tags, fields = points[0][1], points[0][2]
        self.assertEqual(tags["label"], "")
        self.assertNotIn("ratio", fields)
        self.assertNotIn("label=", de.to_line(*points[0], TS))

    def test_no_torrents(self):
        points = de.build_points({"ui": {"torrents": None, "stats": STATS}}, TS, False, de.StateCoder())
        self.assertEqual([p[0] for p in points], ["stats"])
        self.assertEqual(points[0][2]["torrents_count"], 0)

    def test_state_codes_and_unknown_logged_once(self):
        coder = de.StateCoder()
        self.assertEqual([coder(s) for s in de.STATE_CODES], list(range(1, 9)))
        with self.assertLogs("deluge-exporter", level="WARNING") as logs:
            self.assertEqual(coder("Mystery"), -1)
            self.assertEqual(coder("Mystery"), -1)
        self.assertEqual(len(logs.output), 1)


class FakeDeluge:
    def __init__(self, answer):
        self.answer = answer

    def fetch(self):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class FakeWriter:
    def __init__(self):
        self.lines = []

    def write(self, lines):
        self.lines.extend(lines)


def config(**env):
    base = {"DELUGE_URL": "http://deluge:8112", "INFLUXDB_URL": "http://influx:8086"}
    base.update(env)
    return de.Config(base)


class ExporterTests(unittest.TestCase):
    def test_once_success(self):
        writer = FakeWriter()
        code = de.Exporter(config(YEAR_TAG="true"), FakeDeluge(result()), writer).run(once=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(writer.lines), 3)
        self.assertTrue(writer.lines[0].startswith("torrent,hex=aa11,label=linux,name=Some\\ Linux\\ ISO,"
                                                   "tracker_host=example.org,year=2026 "))
        self.assertTrue(writer.lines[1].startswith("stats,year=2026 "))

    def test_once_failure_is_logged_not_raised(self):
        with self.assertLogs("deluge-exporter", level="WARNING") as logs:
            code = de.Exporter(config(), FakeDeluge(de.DelugeError("auth.login rejected the password")),
                               FakeWriter()).run(once=True)
        self.assertEqual(code, 1)
        self.assertIn("cycle failed: DelugeError", logs.output[0])


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        c = config()
        self.assertEqual(c.deluge_url, "http://deluge:8112/json")
        self.assertEqual(c.deluge_password, "deluge")
        self.assertEqual((c.influxdb_database, c.interval, c.year_tag), ("deluge", 30, False))

    def test_url_with_json_suffix_kept(self):
        self.assertEqual(config(DELUGE_URL="https://x/deluge/json/").deluge_url, "https://x/deluge/json")

    def test_invalid(self):
        for env in ({"DELUGE_URL": ""}, {"INFLUXDB_URL": ""}, {"YEAR_TAG": "maybe"}, {"INTERVAL": "0"}):
            with self.assertRaises(de.ConfigError):
                config(**env)

    def test_main_exits_2_on_bad_config(self):
        self.assertEqual(de.main(["--once"], env={}), 2)

    def test_influxdb2(self):
        c = config(INFLUXDB_TOKEN="t0k", INFLUXDB_ORG="home")
        self.assertEqual((c.influxdb_org, c.influxdb_bucket), ("home", "deluge"))
        self.assertEqual(config(INFLUXDB_TOKEN="t", INFLUXDB_ORG="o", INFLUXDB_BUCKET="b").influxdb_bucket, "b")
        with self.assertRaises(de.ConfigError):
            config(INFLUXDB_TOKEN="t0k")


class HealthcheckTests(unittest.TestCase):
    def setUp(self):
        if os.path.exists(de.HEARTBEAT_FILE):
            os.remove(de.HEARTBEAT_FILE)

    def check(self, env=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = de.healthcheck(env or {})
        return code, out.getvalue()

    def stamp(self, seconds_ago):
        with open(de.HEARTBEAT_FILE, "w") as f:
            f.write(repr(time.time() - seconds_ago))

    def test_no_heartbeat_is_unhealthy(self):
        self.assertEqual(self.check()[0], 1)

    def test_fresh_and_stale(self):
        self.stamp(10)
        self.assertEqual(self.check(), (0, "healthy: last successful write 10s ago\n"))
        self.stamp(100)  # default INTERVAL 30: limit 90s
        code, text = self.check()
        self.assertEqual(code, 1)
        self.assertIn("limit 90s", text)
        self.assertEqual(self.check({"INTERVAL": "60"})[0], 0)  # limit 180s

    def test_bad_interval_or_file_is_unhealthy(self):
        self.stamp(1)
        self.assertEqual(self.check({"INTERVAL": "ten"})[0], 1)
        with open(de.HEARTBEAT_FILE, "w") as f:
            f.write("garbage")
        self.assertEqual(self.check()[0], 1)

    def test_main_healthcheck_needs_no_other_settings(self):
        self.stamp(1)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(de.main(["--healthcheck"], env={}), 0)

    def test_success_writes_heartbeat_failure_does_not(self):
        de.Exporter(config(), FakeDeluge(result()), FakeWriter()).run(once=True)
        self.assertEqual(self.check()[0], 0)
        os.remove(de.HEARTBEAT_FILE)
        with self.assertLogs("deluge-exporter", level="WARNING"):
            de.Exporter(config(), FakeDeluge(de.DelugeError("down")), FakeWriter()).run(once=True)
        self.assertFalse(os.path.exists(de.HEARTBEAT_FILE))

    def test_unwritable_heartbeat_does_not_raise(self):
        saved = de.HEARTBEAT_FILE
        de.HEARTBEAT_FILE = os.path.join(_HEARTBEAT_DIR.name, "missing-dir", "heartbeat")
        try:
            de.write_heartbeat()
        finally:
            de.HEARTBEAT_FILE = saved


class StubHandler(http.server.BaseHTTPRequestHandler):
    """A Deluge Web UI: auth.login sets a session cookie that the other methods require."""
    password = "secret"
    calls = []
    blocklist = True        # False: answer like Deluge without the plugin
    session_error = False   # True: core.get_session_status fails

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        if self.path.startswith(("/write", "/api/v2/write")):
            StubHandler.calls.append(("write", self.path, body, self.headers.get("Authorization")))
            return self._send(204, "")
        rpc = json.loads(body)
        cookie = self.headers.get("Cookie", "")
        StubHandler.calls.append((rpc["method"], cookie))
        headers = {}
        if rpc["method"] == "auth.login":
            ok = rpc["params"] == [StubHandler.password]
            answer = {"id": rpc["id"], "result": ok, "error": None}
            if ok:
                headers["Set-Cookie"] = "_session_id=abc123; Path=/"
        elif "_session_id=abc123" not in cookie:
            answer = {"id": rpc["id"], "result": None, "error": {"message": "Not authenticated", "code": 1}}
        elif rpc["method"] == "web.update_ui":
            answer = {"id": rpc["id"], "result": ui(), "error": None}
        elif rpc["method"] == "core.get_session_status":
            answer = ({"id": rpc["id"], "result": None, "error": {"message": "boom", "code": 2}}
                      if StubHandler.session_error else
                      {"id": rpc["id"], "result": {k: SESSION[k] for k in rpc["params"][0] if k in SESSION},
                       "error": None})
        elif rpc["method"] == "blocklist.get_status":
            answer = ({"id": rpc["id"], "result": BLOCKLIST, "error": None} if StubHandler.blocklist else
                      {"id": rpc["id"], "result": None, "error": {"message": "Unknown method", "code": 2}})
        else:
            answer = {"id": rpc["id"], "result": True, "error": None}
        self._send(200, json.dumps(answer), headers)

    def _send(self, code, body, headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body.encode())


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), StubHandler)
        cls.url = "http://127.0.0.1:{0}".format(cls.server.server_port)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        StubHandler.calls.clear()
        StubHandler.blocklist = True
        StubHandler.session_error = False

    def methods(self):
        return [c[0] for c in StubHandler.calls]

    def test_login_fetch_logout_with_session_cookie(self):
        data = de.DelugeClient(self.url + "/json", "secret").fetch()
        self.assertEqual(data["ui"]["torrents"]["aa11"]["state"], "Seeding")
        self.assertEqual(data["session"], SESSION)
        self.assertEqual(data["blocklist"]["num_blocked"], 2938)
        self.assertEqual(self.methods(), ["auth.login", "web.update_ui", "core.get_session_status",
                                          "blocklist.get_status", "auth.delete_session"])
        self.assertTrue(all("_session_id=abc123" in c[1] for c in StubHandler.calls[1:]))

    def test_blocklist_plugin_disabled_is_asked_once(self):
        StubHandler.blocklist = False
        client = de.DelugeClient(self.url + "/json", "secret")
        with self.assertLogs("deluge-exporter", level="INFO") as logs:
            self.assertIsNone(client.fetch()["blocklist"])
        self.assertIn("Blocklist plugin is not enabled", logs.output[0])
        client.fetch()
        self.assertEqual(self.methods().count("blocklist.get_status"), 1)

    def test_failing_session_status_does_not_fail_the_fetch_and_logs_once(self):
        StubHandler.session_error = True
        client = de.DelugeClient(self.url + "/json", "secret")
        with self.assertLogs("deluge-exporter", level="WARNING") as logs:
            first = client.fetch()
            client.fetch()
        self.assertIsNone(first["session"])
        self.assertEqual(first["ui"]["torrents"]["aa11"]["state"], "Seeding")
        self.assertEqual(len(logs.output), 1)
        StubHandler.session_error = False
        with self.assertLogs("deluge-exporter", level="INFO") as logs:
            self.assertEqual(client.fetch()["session"], SESSION)
        self.assertIn("working again", logs.output[0])

    def test_wrong_password(self):
        with self.assertRaises(de.DelugeError):
            de.DelugeClient(self.url + "/json", "wrong").fetch()
        self.assertEqual([c[0] for c in StubHandler.calls], ["auth.login"])

    def test_sessions_are_not_shared_between_fetches(self):
        client = de.DelugeClient(self.url + "/json", "secret")
        client.fetch()
        client.fetch()
        second_login = [c for c in StubHandler.calls if c[0] == "auth.login"][1]
        self.assertEqual(second_login, ("auth.login", ""))  # second login starts with no cookie

    def test_influx_write(self):
        de.InfluxWriter(self.url, "deluge", "u", "p").write(["m v=1i 1"])
        self.assertEqual(StubHandler.calls[0], ("write", "/write?db=deluge&precision=ms", "m v=1i 1", "Basic dTpw"))

    def test_influxdb2_write(self):
        de.InfluxWriter(self.url, "deluge", token="t0k", org="home", bucket="torrents").write(["m v=1i 1"])
        self.assertEqual(StubHandler.calls[0], ("write", "/api/v2/write?org=home&bucket=torrents&precision=ms",
                                                "m v=1i 1", "Token t0k"))


if __name__ == "__main__":
    unittest.main()
