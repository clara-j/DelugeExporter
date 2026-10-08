# Deluge Exporter

Polls the [Deluge](https://deluge-torrent.org) Web UI's JSON-RPC API and writes per-torrent,
session and blocklist metrics: state, progress, swarm health, transfer rates, ratio, seeding time,
tracker errors and disk use for every torrent, plus Deluge's session counters.

**Designed to work with InfluxDB 1.x, InfluxDB 2.x and VictoriaMetrics**, writing the same data to
each:

| Store | How it writes | Configure with |
|---|---|---|
| InfluxDB 1.x | `POST /write?db=…` | `INFLUXDB_URL`, `INFLUXDB_DATABASE`, optional `INFLUXDB_USERNAME`/`INFLUXDB_PASSWORD` |
| InfluxDB 2.x | `POST /api/v2/write?org=…&bucket=…` with an API token | `INFLUXDB_URL`, `INFLUXDB_TOKEN`, `INFLUXDB_ORG`, `INFLUXDB_BUCKET` |
| VictoriaMetrics | `POST /write?db=…`. The database name becomes a `db` label. | `INFLUXDB_URL` (port 8428), `INFLUXDB_DATABASE` |

All three have been tested against a live Deluge 2.2.0. The schema follows two rules so that it
also suits VictoriaMetrics, which stores numbers only:
- **Numbers, not text.** Every value is numeric, except one text field kept for InfluxDB, and that
  one has a numeric twin.
- **Fixed types.** A field never switches between integer and float. InfluxDB fixes a field's type
  on first write and rejects a change.

The exporter is a single small container using only the Python standard library.

## Quick start

Images for `linux/amd64` and `linux/arm64` are published to the GitHub Container Registry.

InfluxDB 1.x or VictoriaMetrics:

```sh
docker run -d --name deluge-exporter --restart unless-stopped \
  -e DELUGE_URL=http://deluge:8112 \
  -e DELUGE_PASSWORD=your-web-ui-password \
  -e INFLUXDB_URL=http://influxdb:8086 \
  -e INFLUXDB_DATABASE=deluge \
  ghcr.io/clara-j/delugeexporter:latest
```

For VictoriaMetrics, point `INFLUXDB_URL` at it (`http://victoriametrics:8428`); no database needs
creating. For InfluxDB 1.x, create the database first:

```sh
curl -XPOST http://influxdb:8086/query --data-urlencode 'q=CREATE DATABASE "deluge"'
```

InfluxDB 2.x (create the bucket first; the token needs write access to it):

```sh
docker run -d --name deluge-exporter --restart unless-stopped \
  -e DELUGE_URL=http://deluge:8112 \
  -e DELUGE_PASSWORD=your-web-ui-password \
  -e INFLUXDB_URL=http://influxdb2:8086 \
  -e INFLUXDB_TOKEN=your-api-token \
  -e INFLUXDB_ORG=your-org \
  -e INFLUXDB_BUCKET=deluge \
  ghcr.io/clara-j/delugeexporter:latest
```

Deluge's **Web UI** must be enabled, connected to a daemon, and reachable on its port (8112 by
default). `DELUGE_PASSWORD` is the Web UI password. Deluge 2.x is required; it was tested with
2.2.0 on libtorrent 2.0.

### Image tags

| Tag | Contents |
|---|---|
| `latest` | The current `main` branch. Rebuilt on every push to `main`. |
| `1.0.0` | One exact release. Never changes. |
| `1.0` | The newest `1.0.x` release. Picks up fixes, not new minor versions. |

### Docker Compose

```yaml
services:
  deluge-exporter:
    image: ghcr.io/clara-j/delugeexporter:latest
    container_name: deluge-exporter
    restart: unless-stopped
    environment:
      DELUGE_URL: http://deluge:8112
      DELUGE_PASSWORD: ${DELUGE_PASSWORD}   # keep secrets in an .env file, not here
      INFLUXDB_URL: http://influxdb:8086
      INFLUXDB_DATABASE: deluge
      INTERVAL: "30"
```

### Building the image yourself

```sh
git clone https://github.com/clara-j/DelugeExporter.git
cd DelugeExporter
docker build -t deluge-exporter .
```

## How often it polls

Every **`INTERVAL` seconds, 30 by default**. Set it to any whole number of seconds, for example
`INTERVAL=60` to poll once a minute. Each poll is one cycle, which takes well under a second:

1. log in to the Web UI;
2. read every torrent and the session statistics (`web.update_ui`);
3. read the libtorrent session counters (`core.get_session_status`);
4. read the Blocklist plugin's status (`blocklist.get_status`), if the plugin is enabled;
5. log out, so no session stays open between polls;
6. write everything in one request, with one timestamp at millisecond precision.

The interval is measured from the start of one cycle to the start of the next, so a slow cycle
does not push later polls back.

## Configuration

Everything is set by environment variable.

| Variable | Default | Description |
|---|---|---|
| `DELUGE_URL` | *required* | Web UI URL, e.g. `http://deluge:8112` or `https://example.com/deluge`. `/json` is added if it is not already there. |
| `DELUGE_PASSWORD` | `deluge` | Web UI password. While it is Deluge's default, a warning is logged at startup: anyone who can reach the Web UI can log in with it. |
| `DELUGE_VERIFY_SSL` | `true` | Set `false` to accept a self-signed certificate on an `https` Web UI. |
| `INTERVAL` | `30` | Seconds between polls. |
| `INFLUXDB_URL` | *required* | Base URL of InfluxDB or VictoriaMetrics, e.g. `http://influxdb:8086`. |
| `INFLUXDB_DATABASE` | `deluge` | InfluxDB 1.x database. In VictoriaMetrics it becomes the `db` label. |
| `INFLUXDB_USERNAME`, `INFLUXDB_PASSWORD` | *(none)* | InfluxDB 1.x credentials, sent as HTTP basic auth. Optional. |
| `INFLUXDB_TOKEN` | *(none)* | InfluxDB 2.x API token. Setting it switches to the InfluxDB 2 write API. |
| `INFLUXDB_ORG` | *(none)* | InfluxDB 2.x organization. Required with `INFLUXDB_TOKEN`. |
| `INFLUXDB_BUCKET` | `INFLUXDB_DATABASE` | InfluxDB 2.x bucket. |
| `YEAR_TAG` | `false` | `true` adds a `year` tag to every point; see below. |
| `LOG_LEVEL` | `INFO` | `DEBUG` adds one line per cycle. |

Booleans accept `true`/`false`, `1`/`0`, `yes`/`no` and `on`/`off`. Any other value, or a missing
required setting, stops the exporter at startup with a message.

### Command line

```
python deluge_exporter.py [--once] [--log-level LEVEL] [--version]
```

`--once` runs a single cycle and exits: `0` on success, `1` on failure. Configuration errors exit
`2`.

## Metrics

Three measurements. In VictoriaMetrics each field becomes a series named `<measurement>_<field>`,
for example `torrent_seeding_time`.

### `torrent`: one point per torrent

| Tag | |
|---|---|
| `hex` | Info hash: the torrent's stable identity |
| `name` | Torrent name |
| `label` | Label from Deluge's Label plugin. Left out when the torrent has none. |
| `tracker_host` | Tracker host name |

**State and progress**

| Field | Type | Meaning |
|---|---|---|
| `state` | string | Deluge's state text: `Seeding`, `Downloading`, … Kept for InfluxDB; VictoriaMetrics stores it as `0`. |
| `state_code` | integer | The same state as a number; see the table below |
| `progress` | float | Percent complete, 0–100 |
| `eta` | integer | Seconds to completion. `0` when not downloading. |
| `total_size` | integer | Size of the whole torrent, in bytes |
| `total_wanted` | integer | Bytes selected for download. Smaller than `total_size` when some files are skipped. |
| `total_remaining` | integer | Wanted bytes still to download |
| `total_done` | integer | Bytes downloaded and verified |

**Swarm and health**

| Field | Type | Meaning |
|---|---|---|
| `num_seeds`, `num_peers` | integer | Seeds and peers connected to |
| `total_seeds`, `total_peers` | integer | Seeds and peers in the swarm, as reported by the tracker |
| `distributed_copies` | float | Complete copies available across connected peers. **Below 1.0, no reachable peer has every piece, so the torrent cannot finish.** |
| `last_seen_complete` | integer | Unix time a complete copy was last seen in the swarm. `0` means never. |
| `tracker_ok` | integer | `0` while the tracker reports an error or warning, such as `Error: unregistered torrent` (common when a private tracker removes a torrent). `1` otherwise, including `Announce OK` and `Announce Sent`. |
| `message_ok` | integer | `0` when Deluge flags the torrent with an error, such as missing files; `1` otherwise |
| `private` | integer | `1` for a private-tracker torrent |

**Transfer and time**

| Field | Type | Meaning |
|---|---|---|
| `download_payload_rate`, `upload_payload_rate` | integer | Bytes per second, payload only |
| `total_uploaded` | integer | Bytes uploaded in total. `ratio` is this divided by `total_done`. |
| `ratio` | float | Share ratio. `-1` for a torrent that has downloaded nothing. |
| `time_added` | integer | Unix time the torrent was added |
| `completed_time` | integer | Unix time it finished downloading. `0` if it has not. |
| `active_time` | integer | Seconds the torrent has been active (downloading or seeding) |
| `seeding_time` | integer | Seconds spent seeding. Private trackers often require a minimum. |
| `time_since_upload` | integer | Seconds since it last uploaded anything. `-1` means never. |

Fields Deluge reports as empty for a torrent are left out of that point.

#### State codes

| `state_code` | `state` | Meaning |
|---|---|---|
| 1 | Allocating | Pre-allocating disk space |
| 2 | Checking | Verifying existing data |
| 3 | Downloading | Downloading |
| 4 | Seeding | Complete and seeding |
| 5 | Paused | Paused |
| 6 | Error | Error, e.g. missing files or disk full |
| 7 | Queued | Waiting for a queue slot |
| 8 | Moving | Moving storage |
| -1 | *anything else* | Not in the table. Logged once per state as a warning. |

The order and names follow Deluge 2.x's `deluge.common.TORRENT_STATE`.

### `stats`: one point per cycle

From `web.update_ui`:

| Field | Type | Meaning |
|---|---|---|
| `download_rate`, `upload_rate` | float | Total bytes per second, including protocol overhead |
| `download_protocol_rate`, `upload_protocol_rate` | float | Protocol overhead alone, bytes per second |
| `num_connections` | integer | Open peer connections |
| `dht_nodes` | integer | DHT nodes known. Always `0` when DHT is turned off in Deluge's settings, as it often is for private trackers. |
| `free_space` | integer | Free bytes in the download location |
| `has_incoming_connections` | integer | `1` if peers can connect in, meaning the listening port is reachable |
| `max_download`, `max_upload` | float | Global speed limits in KiB/s; `-1` means unlimited |
| `max_num_connections` | integer | Global connection limit |
| `torrents_count` | integer | Torrents in this cycle |

From libtorrent's session counters (`core.get_session_status`), counted since Deluge started:

| Field | libtorrent counter | Meaning |
|---|---|---|
| `recv_bytes`, `sent_bytes` | `net.recv_bytes`, `net.sent_bytes` | Total bytes received and sent, including protocol overhead |
| `recv_failed_bytes` | `net.recv_failed_bytes` | Bytes received that failed the hash check: corrupt data from peers |
| `peers_connected` | `peer.num_peers_connected` | Connected peers |
| `peers_half_open` | `peer.num_peers_half_open` | Connection attempts in progress |
| `peers_banned_for_hash_failure` | `peer.banned_for_hash_failure` | Peers banned for sending corrupt data |
| `peers_banned` | `peer.num_banned_peers` | Peers currently banned |
| `disk_queued_write_bytes` | `disk.queued_write_bytes` | Bytes waiting to be written. Growth means the disk is the bottleneck. |

These counters only increase, until Deluge restarts. Use rate functions over them, such as
`rate(...)` or InfluxQL `DERIVATIVE()`. `external_ip` is not written: it is text, and as a metric it
would only record your public address.

### `blocklist`: one point per cycle, when the Blocklist plugin is enabled

| Field | Type | Meaning |
|---|---|---|
| `num_blocked` | integer | IP ranges currently blocked |
| `num_whited` | integer | Whitelisted ranges |
| `up_to_date` | integer | `1` if Deluge considers the blocklist up to date |
| `file_date` | integer | Unix time of the blocklist file in use. Alert when it gets old: an auto-update that silently stopped shows up here. |

If Deluge reports the plugin is not loaded, the exporter logs it once and stops asking until it
restarts.

### The `year` tag (`YEAR_TAG=true`)

Adds `year`, the **UTC** calendar year of the point's timestamp (`2026`, `2027`, …), to every
point. This is for stores that keep one retention period for everything and can only delete whole
series, such as VictoriaMetrics' community edition. With the tag, each year is a separate set of
series, so an old year can be deleted on its own:

```sh
curl --data-urlencode 'match[]={db="deluge",year="2024"}' http://victoriametrics:8428/api/v1/admin/tsdb/delete_series
```

Series restart every 1 January, so aggregate (`sum`, `avg`, `max by (…)`) in dashboards rather than
plotting raw series across New Year. Leave the tag off for InfluxDB, which has proper retention
policies.

### Example queries

MetricsQL / PromQL (VictoriaMetrics, Prometheus):

```promql
# Torrents per state
count by (state_code) (torrent_state_code{db="deluge"})

# Torrents that cannot finish: downloading, but no full copy reachable
torrent_distributed_copies{db="deluge"} < 1 and on(hex) torrent_state_code{db="deluge"} == 3

# Tracker problems, e.g. a torrent removed from a private tracker
torrent_tracker_ok{db="deluge"} == 0

# Hours seeded per torrent
torrent_seeding_time{db="deluge"} / 3600

# Disk used per label
sum by (label) (torrent_total_size{db="deluge"})

# Blocklist file age in days
(time() - blocklist_file_date{db="deluge"}) / 86400

# Corrupt data received, per hour
increase(stats_recv_failed_bytes{db="deluge"}[1h])
```

InfluxQL (InfluxDB 1.x):

```sql
SELECT last("ratio") FROM "torrent" WHERE $timeFilter GROUP BY "name"
SELECT non_negative_derivative(last("sent_bytes"), 1s) FROM "stats" WHERE $timeFilter GROUP BY time($__interval)
```

Flux (InfluxDB 2.x):

```flux
from(bucket: "deluge")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "torrent" and r._field == "tracker_ok")
  |> last()
  |> filter(fn: (r) => r._value == 0)
```

## Load and cardinality

- **API load:** each cycle makes five Web UI requests (four without the Blocklist plugin). At the
  default `INTERVAL` of 30 seconds, that is 10 requests a minute.
- **Series:** about 26 per torrent, 20 for `stats` and 4 for `blocklist`. 54 torrents come to about
  1,430. A torrent added or removed starts or ends its own series. With `YEAR_TAG=true`, all of
  them restart each January.

## Logging

One line per event, written to stdout:
- one `INFO` line at startup, naming the target and the interval;
- a `WARNING` at startup while the default password is in use;
- a `WARNING` for each failed cycle, with the consecutive failure count;
- an `ERROR` after 10 consecutive failures;
- an `INFO` line on recovery;
- an hourly `INFO` summary of cycles, failures and torrents;
- a `WARNING` once for each unknown torrent state;
- a `WARNING` once when the session-counter call starts failing (torrent data is still written),
  and an `INFO` line when it works again;
- an `INFO` line once when the Blocklist plugin turns out not to be enabled.

Passwords and tokens are never logged.

## Releases

`.github/workflows/docker-publish.yml` runs the tests on every push and pull request. When they
pass, it publishes images:

- **Push to `main`:** builds and publishes `latest`.
- **Push a version tag** (`git tag v1.0.1 && git push origin v1.0.1`): also publishes the `X.Y.Z`
  and `X.Y` tags.

Keep `__version__` in `deluge_exporter.py` in step with the tag. A package's first publish can
default to **private**. Check it once under the repository's **Packages → Package settings →
Change visibility**.

## Development

The tests use the standard library only:

```sh
python3 -m unittest discover -s tests -v
```

They need no Deluge or InfluxDB. Fixtures stand in for API responses, and the HTTP clients run
against a local stub server that implements the Web UI's login-cookie flow, the optional calls and
both write APIs.
