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
   `ontime/matching.py`, three tiers with a position tiebreak.
3. **The feed is full of stale ghosts.** Vehicles persist for hours after their journey
   ends because operators do not signal completion. Records observed 8+ hours old
   alongside fresh ones. Always filter on `RecordedAtTime` (`ONTIME_STALE_SECS`).
4. **Use the prebuilt regional GTFS**, not TransXChange:
   `https://data.bus-data.dft.gov.uk/timetable/download/gtfs-file/north_west/`
   89MB zipped, 544MB unpacked, rebuilt daily. `stop_times.txt` alone is 398MB —
   always stream it out of the zip, never unpack or read it whole.
5. **GTFS times exceed 24h** for post-midnight trips. Service-day handling lives in
   `matching.service_day_offsets`.
6. **The flake disables `fastapi`'s check phase.** Nixpkgs has no darwin cache for it,
   so it builds from source and its test suite pulls in scipy — an hour-plus build.
   Skipping checks takes the build list from 12 derivations to 3, `nix develop` to 12s.
   Do not "helpfully" re-enable it.
7. **Indices are sparse on purpose.** Four unused ones were removed after checking
   EXPLAIN QUERY PLAN; one on `observations` cost 45% more insert time for nothing.
   Inserting is the hot path. Measure with `scripts/benchmark.py` before adding any.
8. **`derive_stop_events` scans only 26 hours** and is idempotent. Do not widen it to
   the full retention window — that reloads hundreds of thousands of rows hourly to
   rewrite results that cannot have changed. Tests pass `WIDE_LOOKBACK_HOURS`.

## Measured baselines (Apple silicon, real data)

Ingest 17s · cache 8.9MB · match 1.41ms/vehicle · duty cycle <3% at a 15s poll ·
RSS 60MB · history ~8MB/day. Docker image 220MB, non-root, no pip. 102 tests in ~2s.

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
