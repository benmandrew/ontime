# ontime

A private live-departures dashboard for three bus stops in Manchester, built on the Department for Transport (DfT) *Bus Open Data Service* (BODS).

The stops watched by default:

| NaPTAN | ATCO | Stop | Services |
|---|---|---|---|
| `MANADGMT` | `1800EB01881` | Hyde Grove, Plymouth Grove (westbound) | 50, 197, 53, 41, 191, 51 |
| `MANGPWTD` | `1800SB13961` | Swinton Grove, Upper Brook Street (Stop L) | 50, 197, 53, 41, 191, 51 |
| `MANADTDW` | `1800EB06241` | Cavanagh Close, Stockport Road (northwest-bound) | 192 |

Together these see 1,135 scheduled calls per day, of which service 192 alone accounts for 762.

## Why arrival times are computed here

BODS publishes vehicle positions in *Standard Interface for Real-time Information* (SIRI) format, specifically the Vehicle Monitoring profile, SIRI-VM. The standard permits a `MonitoredCall` element carrying an `ExpectedArrivalTime` for each stop ahead. Greater Manchester publishers do not populate it. A sample of 417 vehicles across the conurbation contained no `MonitoredCall`, no `OnwardCalls` and no `ExpectedArrivalTime` of any kind, and the *General Transit Feed Specification – Realtime* (GTFS-RT) mirror exposes only `VehiclePositions` with no `TripUpdates`.

The feed therefore answers *where is the bus* and never *when will it reach my stop*. Everything on this dashboard beyond a map pin is derived locally, by matching each vehicle to a timetabled trip and walking the remaining stop sequence.

## How prediction works

**Filter by direction.** A route's two directions are separate trips, and only one direction is cached when a watched stop is served one way: all 467 cached trips for the 192 run towards Manchester, because MANADTDW is the northwest-bound stop. Twenty-three of thirty-seven live 192s were heading to Stockport. A vehicle is kept only when its `DestinationRef` appears *after* its current position in the candidate trip. Comparing terminus identifiers alone is not enough, because a terminus often shares one ATCO code between directions — Hazel Grove Park and Ride does.

**Match.** Each surviving vehicle is tied to a timetabled trip. The obvious key does not work: BODS ships a `vehicle_journey_code` in its converted GTFS and a `DatedVehicleJourneyRef` in SIRI-VM, and the converter regenerates the former rather than preserving the operator's. Measured against the services calling at these stops, service 50 matched 0 of 16 live vehicles. What survives conversion is the shape of the journey, so matching keys on origin stop, destination stop and aimed departure time, loosening through three tiers until something fits. Ties break on departure time, never on distance: every trip on a route follows one road, so on a frequent corridor the whole candidate set sits within metres of the vehicle and the closest path is arbitrary.

**Say only what is known.** A match that rests on geometry alone places the vehicle on the route but does not identify which run it is on. The arrival still holds, because every trip walks the same stops, but the scheduled time would come from an arbitrary run, so no delay is reported rather than a fabricated one. A delay beyond 90 minutes is treated the same way: that is a mismatched trip, not a late bus.

**Locate.** The closest stop in the matched trip's sequence gives the vehicle's progress, and its distance along the current leg prorates the partial segment.

**Sum.** Remaining segments are added up. Because the sum starts from where the bus is rather than where it should be, lateness needs no separate correction — a bus twenty minutes behind is simply twenty minutes further back in the sequence.

**Learn.** Every polled position is stored. Once a run goes quiet, positions reduce to one closest-approach event per stop, and those events aggregate into median traversal times per segment, per hour, per day type. Segments with at least 5 samples use the observed median; everything else falls back to the timetabled gap. Rows on the dashboard are tagged so the two are distinguishable.

Raw positions are trimmed to `ONTIME_RETAIN_DAYS` (21 by default). The aggregates derived from them are kept permanently, so accuracy improves while the database stays bounded.

## Running it

### Development

The flake pins the whole toolchain, so `direnv allow` is the only setup step:

```sh
cp .env.example .env      # add the BODS key
chmod 600 .env
direnv allow              # or: nix develop

python -m ontime.ingest   # build the timetable cache, roughly 4 minutes
python -m ontime.web      # http://127.0.0.1:8000
```

Registration for a free key takes a minute at [data.bus-data.dft.gov.uk](https://data.bus-data.dft.gov.uk/account/signup/).

Entering the shell takes about 10 seconds and builds two derivations. It briefly took considerably longer: nixpkgs has no binary cache coverage for `fastapi` on `aarch64-darwin`, so it built from source, and running its upstream test suite pulled in `scipy`. Dropping fastapi for starlette removed the cause rather than papering over it.

### Docker, behind Tailscale

The compose file runs two services over one named volume. The dashboard polls and predicts; the maintenance loop refreshes the 89MB timetable archive daily and relearns segments hourly, so a download never stalls the departure board.

```sh
docker compose up -d --build
```

Both services share the named `ontime-data` volume, which is deliberate rather than incidental. Two processes on one SQLite database is fine on a real filesystem in write-ahead logging mode, and the web container polling while the maintenance container rebuilds was verified to stay healthy. Substituting a bind mount is not safe on macOS: concurrent access over a Docker Desktop bind mount produced a `SIGBUS` during a rebuild.

The published port binds to `127.0.0.1` deliberately — nothing reaches the local network. Putting it on the tailnet is a separate, explicit step:

```sh
tailscale serve --bg 8000
tailscale serve status
```

That gives an HTTPS URL on the tailnet with a certificate issued automatically, reachable only by devices signed into the same account. To withdraw it, `tailscale serve --https=443 off`. Avoid `tailscale funnel` here: it publishes to the open internet, and this application has no authentication of its own.

## Resource usage

Measured on Apple silicon against the real feed and the full North West archive, with `python scripts/benchmark.py`:

| | |
|---|---|
| Timetable ingest | 17 s, from an 89MB archive with a 398MB `stop_times.txt` |
| Cache on disk | 8.9 MB for 1,135 trips and 54,131 stop times |
| Vehicle-to-trip matching | 0.32 ms per vehicle over 61 live vehicles |
| Whole poll cycle at 60 vehicles | about 430 ms, of which 400 ms is waiting on the network |
| Duty cycle at a 15-second poll | under 3% |
| Resident memory | 60 MB |
| Position history | 6,018 rows a day at 95 bytes, so 0.6 MB a day and 12 MB at the 21-day retention |
| Container image | 62.4 MB uncompressed, 19.6 MB transferred |

Matching is cheap because the direction filter runs first and cuts the candidate pool hard, after which origin, destination and aimed departure time identify the run uniquely: every match in a live sample landed in the first tier. Adding that filter made matching four times faster than the version without it, not slower, because the expensive positional work now runs against a fraction of the candidates. The destination check itself resolves the vehicle's position once per vehicle rather than once per candidate, which turns a haversine over every candidate's every stop into two dictionary lookups.

Storage is measured rather than extrapolated. Assuming every vehicle visible now reports all day overstates it by more than an order of magnitude, because the feed repeats a vehicle's `RecordedAtTime` between updates and those duplicates are dropped on insert.

Indices are deliberately sparse. Each was checked with `EXPLAIN QUERY PLAN` against a real cache, and four that no query reached were removed: one unused index on `observations` alone cost 45% more insert time, 430 ms against 297 ms per 200,000 rows, and inserting is the hot path. The remaining full-table scans are intentional, either because the caller wants every row or because the table holds a few hundred of them.

Reducing raw positions to per-stop events scans only the last 26 hours. That step is idempotent, so older positions have already been reduced, and rescanning the full retention window hourly would mean loading hundreds of thousands of rows to rewrite results that cannot have changed.

### Image size

The image went from 220 MB to 62.4 MB in three steps, and gzips to 19.6 MB, which is what a pull actually transfers.

**Base.** Debian slim was 109 MB of the original against a 14 MB virtualenv, so the base was almost the whole cost. Alpine took it to 97 MB. Every dependency publishes a musllinux wheel and `--only-binary=:all:` enforces that, so a missing one fails the build rather than quietly compiling Rust.

**Framework.** Nothing here used a fastapi feature — no request models, no dependency injection, no generated schema — only routing and responses, which belong to starlette underneath. The pydantic it pulled in was 8.6 MB of the virtualenv and the only compiled dependency in the image. Starlette was already present as a transitive dependency, so the swap removed 12 MB and a portability risk together, and took the image to 85.9 MB.

**Layers.** The last 23 MB came from understanding why the first attempt at trimming did nothing. Deleting a file that arrived in a lower layer reclaims no space: it writes a whiteout and the original bytes stay. Pruning the interpreter in a `FROM python:3.12-alpine` runtime stage measurably made things worse — 336 kB of whiteouts on top of a 48.1 MB CPython layer that still held every byte. Moving the pruning into the builder, which is discarded, and starting the runtime from bare Alpine so it copies only what survived, is what actually reclaimed it.

What remains is close to the floor for a CPython application: roughly 20 MB of interpreter and standard library, 12 MB of musl and OpenSSL and SQLite, 9 MB of Alpine, 7 MB of `libpython`, 6 MB of virtualenv and 2 MB of timezone data. Going further would mean pruning standard-library C extensions or shipping only the timezones this application names, which trades a little size for a class of import failure that would not show up until it did.

The coupling this buys is recorded in the Dockerfile: `ALPINE_VERSION` must track whatever Alpine `python:3.12-alpine` is built on, or the copied interpreter meets a different musl.

## Logging

Everything goes through `ontime/logs.py` on stdout, with ISO-8601 timestamps in UTC:

```
2026-07-27T00:08:46.198Z INFO     ontime.web       poll: feed=35 matched=31
2026-07-27T00:08:46.199Z ERROR    ontime.web       poll failed: GET /datafeed/?api_key=<redacted> timed out
```

UTC rather than local time, because container logs get read from other timezones and the feed's own `RecordedAtTime` is UTC — a mixed-zone log makes staleness arithmetic needlessly confusing. Uvicorn's own loggers are re-pointed at the same handler so the output stays uniform. `ONTIME_LOG_LEVEL` sets the threshold.

Every record passes a filter that strips the API key. Call sites already redact explicitly; the filter catches anything added later that forgets to.

## Configuration

Every setting is an environment variable, listed in `.env.example`.

| Variable | Default | Meaning |
|---|---|---|
| `BODS_API_KEY` | — | Required. Never committed, never sent to the browser. |
| `ONTIME_BBOX` | `-2.32,53.38,-2.10,53.52` | Feed bounding box, `min_lon,min_lat,max_lon,max_lat`. |
| `ONTIME_POLL_SECS` | `15` | Feed poll interval. The upstream refreshes every 10 seconds. |
| `ONTIME_STALE_SECS` | `180` | Discard positions older than this. |
| `ONTIME_HORIZON_SECS` | `3600` | How far ahead the board looks. |
| `ONTIME_RETAIN_DAYS` | `21` | Raw position retention. Aggregates outlive it. |
| `ONTIME_DATA_DIR` | `./data` | Cache location, set to `/data` in the container. |
| `ONTIME_LOG_LEVEL` | `INFO` | Logging threshold. |

Changing the watched stops means editing `STOPS` in `ontime/config.py` and re-running the ingest.

## Keeping the key safe

The key is low-value — it grants read access to public data with no billing attached — so the realistic risk is someone else exhausting the rate limit against the account rather than damage. The measures here are proportionate to that.

The credential lives only in `.env`, which is gitignored, mode `600`, and excluded from the Docker build context. It is read into the server process and never reaches the browser, because the page calls this application's own endpoints rather than BODS directly. `config.redact()` strips it from anything heading for a log, which matters more than it sounds: `requests` puts the full URL, query string included, into exception messages, and that is the most likely way for a key to end up in a log file. The fixture builder asserts the key never appears in a captured sample.

If it does leak, rotate it in the BODS account settings; there is no way to scope or restrict an existing key.

## Testing

```sh
pytest -q                     # unit and end-to-end
pytest -q -m e2e              # end-to-end only
pytest -q -m live             # hits the real feed, needs a key
ruff check . && ruff format --check .
mypy ontime
```

Fixtures are cut from real published data rather than invented — `tests/fixtures/mini_gtfs.zip` is a 30-trip subset of the BODS North West archive, and `tests/fixtures/siri_sample.xml` is a recorded live response. `scripts/build_fixtures.py` regenerates both. Calendar rows in the subset are normalised to run every day between 2020 and 2035 so the suite does not expire.

## Layout

```
ontime/config.py       settings, stop definitions, redaction
ontime/logs.py         timestamped logging and the redaction filter
ontime/db.py           SQLite schema
ontime/ingest.py       GTFS download and cache build
ontime/siri.py         feed fetch and parse
ontime/matching.py     vehicle to trip matching
ontime/history.py      observation storage and segment learning
ontime/eta.py          arrival prediction
ontime/web.py          poller and HTTP API
ontime/maintenance.py  periodic refresh loop
```

Accuracy on a straight corridor such as Upper Brook Street settles within a minute or two once a fortnight of history has accumulated, and degrades in the way anyone who has waited on Stockport Road would expect. A commercial Stop Monitoring feed with a real prediction engine would beat it, though not by as much as the price difference suggests.
