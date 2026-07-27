# ontime — project memory

Private live-departures dashboard for three Manchester bus stops, built on the DfT Bus Open Data Service (BODS). Full detail in @README.md; current state in @PLAN.md.

## Watched stops (set in `ontime/config.py`)

| NaPTAN | ATCO | Stop | Services |
|---|---|---|---|
| MANADGMT | 1800EB01881 | Hyde Grove, Plymouth Grove (W) | 50, 197, 53, 41, 191, 51 |
| MANGPWTD | 1800SB13961 | Swinton Grove, Upper Brook St (Stop L) | 50, 197, 53, 41, 191, 51 |
| MANADTDW | 1800EB06241 | Cavanagh Close, Stockport Rd (NW) | 192 only |

1,135 scheduled calls/day total; the 192 is 762 of them.

## Hard-won facts — do not rediscover these

1. **BODS gives positions only.** Verified across 417 GM vehicles: no `MonitoredCall`,
   no `OnwardCalls`, no `ExpectedArrivalTime`. GTFS-RT mirror has `VehiclePositions`
   but no `TripUpdates`. Every ETA in this project is computed locally.
2. **The journey-code join is broken.** GTFS `vehicle_journey_code` (`vj_89`) does not
   match SIRI `DatedVehicleJourneyRef` (`6053`). Service 50 matched 0/16. The BODS
   TransXChange→GTFS converter regenerates the code. Match on
   (route, origin_ref, dest_ref, origin_aimed_departure_time) instead — see
   `ontime/matching.py`, three tiers tied on departure time (see 14).
3. **The feed is full of stale ghosts.** Vehicles persist for hours after their journey
   ends because operators do not signal completion. Records observed 8+ hours old
   alongside fresh ones. Always filter on `RecordedAtTime` (`ONTIME_STALE_SECS`).
4. **Use the prebuilt regional GTFS**, not TransXChange:
   `https://data.bus-data.dft.gov.uk/timetable/download/gtfs-file/north_west/`
   89MB zipped, 544MB unpacked, rebuilt daily. `stop_times.txt` alone is 398MB —
   always stream it out of the zip, never unpack or read it whole.
5. **GTFS times exceed 24h** for post-midnight trips. Service-day handling lives in
   `matching.service_day_offsets`.
6. **Do not reintroduce fastapi.** It was dropped for starlette: nothing used a fastapi
   feature, its pydantic was 8.6MB of a 14MB venv and the only compiled dependency, and
   nixpkgs has no darwin cache for it so `nix develop` built it from source and pulled in
   scipy — an hour-plus shell. Starlette gives routing and responses; that is all we use.
7. **Indices are sparse on purpose.** Four unused ones were removed after checking
   EXPLAIN QUERY PLAN; one on `observations` cost 45% more insert time for nothing.
   Inserting is the hot path. Measure with `scripts/benchmark.py` before adding any.
8. **`derive_stop_events` scans only 26 hours** and is idempotent. Do not widen it to
   the full retention window — that reloads hundreds of thousands of rows hourly to
   rewrite results that cannot have changed. Tests pass `WIDE_LOOKBACK_HOURS`.
9. **Alpine needs tzdata.** Every service-day calculation goes through
   `ZoneInfo("Europe/London")`. The base ships it, but the Dockerfile installs it
   explicitly so a base change fails the build rather than the arithmetic.
10. **Logs go through `ontime/logs.py`**, never `print`. ISO-8601 UTC, and a filter
    strips the API key from every record as a backstop.
11. **Deleting files from a lower layer reclaims nothing** — it writes a whiteout and
    the bytes stay. Prune in the builder; the runtime is bare alpine and copies only
    what survived. `ALPINE_VERSION` in the Dockerfile must match whatever alpine
    `python:3.12-alpine` is built on, or musl mismatches.
12. **Never bind-mount the data dir on macOS.** Concurrent SQLite access over a
    Docker Desktop bind mount SIGBUSes — WAL mmaps the `-shm` file and two kernels
    mapping it through VirtioFS is fatal. Compose uses a named volume; verified safe
    with the web container polling while ingest rebuilds. `ingest` now refuses to
    start when another writer's heartbeat is fresh (`ontime/locking.py`), because a
    SIGBUS cannot be caught and the crash is otherwise unexplainable.
13. **Filter by direction before matching.** A watched stop served one way caches
    only that direction: all 467 cached 192 trips run towards Manchester. 23 of 37
    live 192s head to Stockport, and a time-only match assigned them to northbound
    runs — the source of "362 minutes early" on the board. Require `DestinationRef`
    to appear *after* the vehicle's position; terminus ATCO codes are shared between
    directions so comparing termini alone is not enough.
14. **Never break a match tie on distance.** Every trip on a route follows one road,
    so the whole candidate set sits within metres and "closest path" is arbitrary.
    Tie on departure time. Distance-based ties produced 20-45 minute phantom delays.
15. **Do not claim a delay that is not known.** A geometry-only match cannot identify
    the run, so `schedule_confident` is False and the delay is withheld; the arrival
    is positional and still holds. Delays past 90 minutes are suppressed as
    mismatches rather than displayed.

## Measured baselines (Apple silicon, real data)

Ingest 17s · cache 8.9MB · match 0.32ms/vehicle · duty cycle <3% at a 15s poll ·
RSS 64MB · history 6,018 rows/day = 0.6MB/day, 12MB at 21 days (measured, not extrapolated). Docker image 62.4MB (19.6MB gzipped; was 220MB), non-root,
no pip/fastapi/pydantic. `nix develop` ~10s. 137 tests in ~2s.

## Constraints

- The BODS key lives only in `.env` (gitignored, mode 600, out of the Docker context).
  It must never reach the browser, a log, or a test fixture. Use `config.redact()` on
  anything logged — `requests` puts the full URL in exception messages.
- Deployment is Docker behind `tailscale serve`. Port binds to `127.0.0.1` only.
  Never `tailscale funnel`: the app has no authentication.
- Learned history is the valuable artefact. It lives in the `ontime-data` volume.
  Raw positions trim at 21 days; `segment_stats` is permanent. Do not drop that table.

## Standards

- Nix flake + direnv is the source of truth for the toolchain. `nix develop` must work.
- `ruff check`, `ruff format --check`, `mypy ontime`, `pytest -q` all clean before commit.
- Fixtures are cut from real published data via `scripts/build_fixtures.py`, never
  invented. Calendar rows are normalised to 2020–2035 so the suite does not expire.
- Prose in docs follows `~/.claude/VOICE.md` (explainer mode for README).
