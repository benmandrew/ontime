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
   always stream it out of the zip, never unpack or read it whole. The download
   cannot be trimmed: 20.5MB of it is `shapes.txt`, which nothing reads, but BODS
   answers a `Range:` request with 200 and no `Accept-Ranges`, so members cannot
   be fetched selectively. The scan is one pass, not two — see 19.
5. **GTFS times exceed 24h** for post-midnight trips, and the service day starts at
   noon minus 12 hours — *not* local midnight. The two differ only on the clock-change
   days, where local midnight put every bus exactly 60 minutes out for the whole day —
   inside the 90-minute guard, so nothing suppressed it. `matching.service_midnight` is
   the anchor; `service_day_offsets` measures both the same-day and previous-day offsets
   against it rather than adding a flat 86,400.
6. **Do not reintroduce fastapi.** It was dropped for starlette: nothing used a fastapi
   feature, its pydantic was 8.6MB of a 14MB venv and the only compiled dependency, and
   nixpkgs has no darwin cache for it so `nix develop` built it from source and pulled in
   scipy — an hour-plus shell. Starlette gives routing and responses; that is all we use.
7. **Indices are sparse on purpose.** Four unused ones were removed after checking
   EXPLAIN QUERY PLAN; `obs_trip` alone cost 102% more insert time for nothing
   (591ms against 1193ms for 200,000 rows). Inserting is the hot path. Measure with
   `scripts/benchmark.py` before adding any. Deleting an index from `db.SCHEMA` does
   not remove it from any database that already exists — `init` only ever runs
   CREATE IF NOT EXISTS, so all four survived in the `ontime-data` volume and the
   saving was never banked. `db.DROPPED_INDICES` drops them by name; add to that
   tuple whenever you take one out of the schema.
8. **`derive_stop_events` scans only 26 hours** and is idempotent. Do not widen it to
   the full retention window — that reloads hundreds of thousands of rows hourly to
   rewrite results that cannot have changed. Tests pass `WIDE_LOOKBACK_HOURS`.
   Idempotence rests on two things. The window is wider than a day, so a (trip, vehicle)
   group can hold two runs; they split on silence over `RUN_GAP_SECS` (3h — longest
   journey 63 min, consecutive days ~22h apart). And the service date comes from the
   run's *last* observation, since the window and trim only remove positions from the
   front; anchoring on the first made a midnight-straddling run migrate to a second date
   and be written twice. Not `Trip.service_date` — callers key that dict on trip_id.
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
    directions so comparing termini alone is not enough. Resolve that position on
    *each* candidate. Borrowing one candidate's anchor stop and demanding it of the
    rest made the filter order-dependent: with a reverse trip sorted first, every
    forward candidate failed and the match was dropped entirely.
14. **Never break a match tie on distance, but do bound it.** Every trip on a route
    follows one road, so the candidate set sits within metres and "closest path" is
    arbitrary — tie on departure time; distance ties gave 20-45 minute phantom delays.
    Distance is still a veto on the *winner*: refs are not proof, and a bus broadcasting
    a finished journey's refs en route to the depot matched at tier 1 from 12km out.
    `MAX_OFF_ROUTE_M` (1000m) rejects it, falling through to the geometric tier rather
    than vanishing. From 39,394 real stop gaps: median 284m, p99 496m, max 1,538m — so
    the worst legitimate reading is 769m, against 2,905m for the depot case.
15. **Do not claim a delay that is not known.** A geometry-only match cannot identify
    the run, so `schedule_confident` is False and the delay is withheld; the arrival
    is positional and still holds. Delays past 90 minutes are suppressed as
    mismatches rather than displayed. A delay of exactly zero is *known* — serialise
    it with `is not None`, never a truth test.
16. **Validate the archive before it displaces the good one.** Without a
    `Content-Length` urllib3 cannot see a truncated download, so a half-received zip
    replaced a working cache and the 20h freshness check then refused to re-fetch it:
    every rebuild raised `BadZipFile` for 20 hours. Check the byte count and open the
    zip before `replace()`.
17. **Feed text is hostile.** `DestinationName` is operator-supplied free text that
    reaches the page. It is escaped at render and the page carries a hash-based CSP;
    do not add an inline `style=` attribute or the policy will reject it. A malformed
    numeric field must cost one vehicle, never the poll — a single bad `Bearing`
    blanked all three stops.
18. **A writer's heartbeat is named per process**, not per kind of work
    (`locking._writer_id`). Naming it after the kind meant a host ingest and the
    container's rebuild shared one record, excluded it as their own, and were
    invisible to each other — the exact pairing fact 12 exists to catch. Long work
    holds it through `locking.writing()`, which refreshes it; a single stamp aged out.
19. **`stop_times.txt` is grouped by trip, so scan it once.** Verified across all
    106,058 trips in the real archive: no trip's rows are interrupted by another's,
    and at most one trip is ever open. `_scan_stop_times` buffers the trip in hand
    (~44 rows) and keeps it only if it called at a watched stop, which replaced two
    full passes. It also splits bytes instead of using `csv.DictReader`, which was
    7.7s of a 7.9s pass — decompressing the whole 398MB member is 0.26s, so parsing
    was the cost, never the zip or the I/O. Only the 54,131 rows kept get decoded.
    Every row carries a quoted `stop_headsign`; the split is bounded at the last
    column read and every column read precedes it, so a comma in that operator free
    text can only corrupt the discarded remainder. A reordered header falls back to
    `csv` rather than caching shifted times — a failed rebuild means a board ageing
    by a day per day, so this path must never simply raise.
20. **There is no published road geometry for these routes.** `shapes.txt` is
    131MB unpacked (the 20.5MB in 4 is its *zipped* size), grouped by shape_id,
    1.82s to scan — and worthless here: all 1,135 watched trips carry an empty
    `shape_id`, as do 66,967 of the feed's 106,058. Do not scan it hoping for
    better. The map's route lines come from the stop sequence instead
    (`web._route_lines`), one per route: the longest variant covers every stop
    its route's other variants call at, bar two on the 192 and two on the 53.
    Learned data cannot help — `segment_stats` holds run *times* and carries no
    coordinate. The only true road geometry available is the `observations`
    breadcrumb trail, which would need real work to clean.
21. **The map's CSP is wider than the board's was, deliberately.** Leaflet is far
    too large to inline and hash, so `script-src`/`style-src` carry `'self'` —
    which makes every route this server answers a candidate `<script src>`. The
    JSON handlers therefore go through `web._json`, which sets `nosniff`, because
    they carry operator free text (17). `img-src` names the tile origin and
    nothing else, derived from `config.MAP_TILE_URL` by `web.tile_origin` so the
    policy and the tile source cannot drift; `data:` is there only because
    Leaflet swaps in a 1x1 data URI when it abandons a tile request. The dark
    basemap is that same origin filtered — `--tile-filter` inverts the tile pane
    and rotates the hue back — not a second, darker tile provider, which would
    mean another host in `img-src` and another party's attribution.
22. **Anything the server serves must be listed in `package-data`.** The image
    installs the package, it does not copy the tree, so `static/vendor/*` had to
    be added or the map would 404 in the container while working perfectly in a
    checkout. A test walks `web.STATIC` against the globs rather than trusting
    memory.
23. **Frame the map on the stops, not the vehicles.** `BBOX` reaches ~20 minutes
    upstream, so fitting the view to stops *and* buses zoomed out to the whole of
    Greater Manchester and left the three watched stops an unreadable speck. Fit
    the stops with `maxZoom: 14`, once, and never refit — re-fitting on a 10s
    poll drags the map out from under anyone who panned it.
24. **Bound anything that re-aggregates history.** `learn_segments` reads all of
    `stop_events` on every hourly pass while holding the write lock, and nothing
    used to delete from that table: 105ms at a month, 1.3s at a year, 4.4s and
    192MB at three. `trim_stop_events` caps it at `STOP_EVENT_RETAIN_DAYS` (90) and
    must run *before* learning, or the pass re-reads the rows it is about to drop.
    `segment_stats` rows are still permanent; they now describe a rolling window.

25. **Judging the model is a separate job from running it.** `segment_stats`
    keeps a median, a p85 and a count, which cannot say whether the count is
    enough; `ontime/segments.py` rebuilds the sample vectors instead, via
    `history.segment_samples` — factored out of `learn_segments` precisely so
    the page and the model cannot drift. Three findings from it, all invisible
    in the stats table. **A 95% interval for a median needs n ≥ 6**: order
    statistics give at best [min, max], covering with probability 1 − 2/2ⁿ, so
    `MIN_SAMPLES = 5` is one short of supporting the claim it implies.
    **Coverage needs a timetable denominator**, not an observed one: 6,771
    buckets are implied, 3.0% seen. Do not compute the CI parametrically —
    traversal times are bounded below by the road and unbounded above by
    traffic. `_scheduled_cells` returns adjacency *separately* from gaps:
    published times round to the minute, so a short hop is often timetabled at
    zero seconds, and folding the two together called four real segments
    unreachable.

26. **Pair stop events only when they are adjacent in the trip's sequence.**
    A run holds only the stops that were detected, so a missed one leaves its
    neighbours side by side in the list while two stops apart on the road.
    `learn_segments` used to pair them, recording a traversal across the gap:
    263 of 484 buckets on one day of real data, every one keyed on stops no
    trip runs consecutively and so unreachable by `eta.predict`, which keys on
    scheduled adjacency. The `5 <= secs <= 1800` guard was written for this and
    cannot do it — a skipped short hop sits inside the window. Requiring
    `s2 == s1 + 1` removed all 263, cost no coverage, and changed no bucket the
    predictor reads. Measured, not assumed: **the detection radius was not the
    cause**. Detections sit at a median 29m from the stop (p90 95m, cap 120m)
    while interior misses sit at a median 1,019m, and 300m would recover only
    10% of them. The cause is discontinuous watching — runs polled with gaps
    under 60s had *zero* interior misses, runs with gaps over 5 minutes had
    81%. `segment_samples` now returns `paired`/`skipped` so the page reports
    that continuity directly. Beware: the local `data/` is ad-hoc dev runs
    (45.7h span, 44.6h of it silent), so its skipped share is not the
    deployment's.

## Measured baselines (Apple silicon, real data)

Rebuild **2.16s** against the real 89.4MB archive (was 17.29s two-pass), the write
lock held for 0.09s of it — the scan runs before a connection opens, which is why the
board no longer 502s nightly. Scan peak RSS 68MB, up from 62MB: the cost of reading
in 1MB chunks. 4MB chunks measured no faster and 27MB worse. Cache 7.6MB · duty cycle
<3% at a 15s poll · history 6,018 rows/day = 0.6MB/day, 12MB at 21 days (measured).
Docker image 62.7MB (was 62.4MB before Leaflet's 162KB), non-root, no
pip/fastapi/pydantic. `nix develop` ~10s. 216 tests in ~4s.

Matching 20 fixture vehicles: 3.35ms before memoisation, **2.00ms** after. Sharing one
haversine per stop id already made the trigonometry cheap — 191 calls — but `_nearest`
was invoked 857 times and re-walked the sequence each time, 40,824 iterations for those
191 distances. `match` now memoises `_nearest` per trip and `dep_delta` per (trip,
service date); the second key must include the date, because `load_trips` emits one
Trip per day and the two variants share a trip_id but not a service midnight.
`scheduled_only` 2.34ms to **0.94ms** via `Trip.target_calls`, which walks the 890 real
calls rather than 40,284 (trip, stop) pairs. `Match.pos_idx` carries the matched
position into `eta.predict`, saving 48 haversines per matched vehicle per poll.

## Constraints

- The BODS key lives only in `.env` (gitignored, mode 600, out of the Docker context).
  It must never reach the browser, a log, or a test fixture. Use `config.redact()` on
  anything logged — `requests` puts the full URL in exception messages.
- Deployment is Docker behind `tailscale serve`. Port binds to `127.0.0.1` only.
  Never `tailscale funnel`: the app has no authentication.
- Learned history is the valuable artefact. It lives in the `ontime-data` volume.
  Raw positions trim at 21 days, derived `stop_events` at 90; `segment_stats` is
  permanent. Do not drop that table.

## Standards

- Nix flake + direnv is the source of truth for the toolchain. `nix develop` must work.
- `ruff check`, `ruff format --check`, `mypy ontime`, `pytest -q` all clean before commit.
- Fixtures are cut from real published data via `scripts/build_fixtures.py`, never
  invented. Calendar rows are normalised to 2020–2035 so the suite does not expire.
- Prose in docs follows `~/.claude/VOICE.md` (explainer mode for README).
