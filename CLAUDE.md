# ontime — project memory

Private live-departures dashboard for four Manchester bus stops, built on the DfT Bus Open Data Service (BODS). Full detail in @README.md; current state in @PLAN.md.

## Watched stops (set in `ontime/config.py`)

| NaPTAN | ATCO | Stop | Watched services (trips in the archive) |
|---|---|---|---|
| MANADGMT | 1800EB01881 | Hyde Grove, Plymouth Grove (W) | 197:83, 191:13, 797:1 |
| MANGPWTD | 1800SB13961 | Swinton Grove, Upper Brook St (Stop L) | 50:226, 41:18, 53:12, 51:7, 751:1 |
| MANADTDW | 1800EB06241 | Cavanagh Close, Stockport Rd (NW) | 192:762 |
| MANGTMGT | 1800SB30631 | University Shopping Centre, Oxford Rd (Stop C) | **41 only** (138), by `Stop.routes` |

1,261 scheduled calls across 1,261 trips and 9 routes; the 192 is 762 of them.
Counts measured against the archive downloaded 2026-07-30; they drift as operators
republish, and the older three-stop table overstated which services reach Hyde Grove
and Swinton Grove by the time it was re-measured.

**Oxford Road is a corridor, and `Stop.routes` is why watching one stop on it is
affordable.** Unrestricted it would cache 2,730 trips on 20 routes against 1,123 on 9
before the stop existed — the 41 alone brings it to 1,261 on the same 9 routes, because
the 41 already served Swinton Grove. Measured three stops / four unrestricted / four
with the 41 limit: `trip_stops` 53,183 / 128,208 / **64,577** rows, cache 7.5MB / 18MB /
**9.1MB**, `load_trips` 107 / 248 / **142**ms, `_scheduled_cells` 225 / 545 / **280**ms,
segment buckets 6,736 / 24,880 / **9,199**, rebuild peak RSS 45.7 / 58.5 / **53.2**MB.

The limit recovers little of the peak — 58.5MB to 53.2MB — because it cannot be applied
until after the scan that sets it (see 29). Pooling inside the scan is what actually cut
memory, and it cut all three configurations by about a fifth.

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
    was the cost, never the zip or the I/O. Only the rows kept get decoded — 128,208
    of 4,746,378 at four stops, against 53,183 at three, which is why the scan grew
    only 3.00s to 3.27s when the watched set went up by a factor of 2.4. Those are
    the scan's own figures, before `_apply_route_limits` prunes them to 64,577; the
    scan cannot apply a route limit because `stop_times.txt` has no route column.
    Every row carries a quoted `stop_headsign`; the split is bounded at the last
    column read and every column read precedes it, so a comma in that operator free
    text can only corrupt the discarded remainder. A reordered header falls back to
    `csv` rather than caching shifted times — a failed rebuild means a board ageing
    by a day per day, so this path must never simply raise.
20. **BODS publishes no road geometry for these routes; OpenStreetMap does.**
    `shapes.txt` is 131MB unpacked (the 20.5MB in 4 is its *zipped* size),
    grouped by shape_id, 1.82s to scan — and worthless here: every watched trip
    carries an empty `shape_id`, re-verified across all 2,730 that MANGTMGT
    reaches unrestricted and so across the 1,261 actually cached, as do 70,155
    of the feed's 111,484. Do not scan it hoping for better, and do not reopen
    TransXChange for it either: `RouteLink/Track/Mapping` exists in the schema,
    but operators [largely do not populate it](https://www.transportapi.com/blog/2023/04/bus-route-geometry-the-most-complete-and-accurate-gb-source/).
    OpenStreetMap carries the same services as `type=route` relations built from
    the roads. `scripts/build_route_shapes.py` matches them to the cached routes
    offline and writes `static/route_shapes.json` (56KB); `ontime/geometry.py`
    reads it and `web._route_lines` prefers it. **Nothing at runtime talks to
    Overpass** — no key, no third-party host, no CSP change, and the ODbL credit
    the tiles already carry covers the geometry too.
    Match on proximity, never on `ref`. Querying the board's box for `ref=41`
    returns a First Manchester service around Ashton whose stops sit a median
    8,598m from anything the watched 41 calls at; the coverage gate drops it at
    0.0% where the two genuine Go North West relations score 85.5% and 82.1%.
    Across the 13 kept relations coverage runs 79.5–98.2% and the median stop
    sits 13–21m from the line, against a 296m chord. One relation per direction
    is what finally separates the 41's Oxford Road and Swinton Grove workings.
    Two things bite when regenerating. **Relation way-chains have holes** — 33
    of them, fifteen at ≤156m then nothing until 371m then kilometres — so
    `MAX_BRIDGE_M` is 200m and the polyline is cut rather than drawn 8,670m
    across Manchester. And **Ramer-Douglas-Peucker has no perpendicular for a
    closed span**: a 41 roundabout taken the whole way round collapsed to two
    identical points until the degenerate case was measured radially.
    `test_no_polyline_has_collapsed_to_a_point` guards the artefact.
    The 751 and the 797 have no relation in the box and keep the stop chords, so
    `_route_lines` must go on supporting both sources. Learning geometry from
    `observations` was considered and rejected: the trail is dense enough
    (scheduled mean 3.80 m/s over 106,264 stop pairs is a breadcrumb every 57m
    at a 15s poll, against a 296m chord) but it only covers `BBOX`, it trims at
    21 days, and cleaning it is map-matching, which needs the road network that
    OpenStreetMap was already going to hand over. `segment_stats` cannot help at
    all — it holds run *times* and carries no coordinate.
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
    **Coverage needs a timetable denominator**, not an observed one, and that
    denominator moved when MANGTMGT was added: 6,736 buckets implied on a weekday
    cache at three stops, 24,880 at four unrestricted, 9,199 under the 41 limit,
    with adjacencies going 420 / 1,046 / 431. The reported coverage percentage
    therefore fell by about a third on the day the stop landed, with nothing lost
    — read a drop across that boundary as a bigger timetable, not as a
    regression. Do not compute the CI parametrically —
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

27. **A trip can call at more than one watched stop — and today none does.**
    True of no cached trip before MANGTMGT, true of 14 of 2,730 with it
    unrestricted (the 13 191s and one 797, each calling at Hyde Grove and then
    at University Shopping Centre), and false again under the 41 limit, which
    drops the 191's Oxford Road call and leaves 1,261 trips holding 1,261 calls.
    Do not read that 1:1 as a guarantee. Lifting the limit, widening it, or
    adding another corridor stop brings it straight back, so the capability is
    tested regardless: `TestTripServingTwoWatchedStops` in `tests/test_e2e.py`
    reproduces it over the 192, the 191 being absent from the cut-down fixture.
    Nothing had to change to support it — the `target_calls` primary key is
    (trip_id, stop_id), `Trip.target_calls` is a list, `eta.predict` is asked
    once per watched stop — but anything that keys a trip's watched call as a
    single value, or stops at the first one it finds, will silently empty one
    stop's board. Note also that `eta.predict` deliberately scans for the
    *first* occurrence of the target rather than reading `trip.index_of`, which
    holds the last — no cached trip calls at one watched stop twice, so the two
    agree, but they would not on a loop route.

28. **A per-stop route limit has to hold in three places, not one.** Dropping
    barred trips at ingest is necessary and not sufficient, because a trip
    cached for an *open* watched stop still runs through the restricted one:
    13 real 191 trips pass University Shopping Centre every weekday on their
    way to Hyde Grove, are matched, and sit in `state.trips`. Without a guard
    both `eta.predict` (via `web.build_board`) and `eta.scheduled_only` (via
    `Trip.target_calls`) will place them at a stop that bars them. All three
    read `config.stop_serves`, which exists so they cannot disagree. Verified
    against the real archive: 152 cached trips still pass through that stop,
    only 138 of them call there, and the board shows 41s and nothing else.

29. **The scan sets the rebuild's high-water mark; nothing after it comes close.**
    Measured with `VmHWM` at each stage: 34MB at import, 67.7MB the moment
    `_scan_stop_times` returns, and unchanged through `_apply_route_limits`, the
    `by_trip` duplicate and the inserts — later stages reuse memory the prune
    freed. Two consequences. Optimising anything downstream of the scan buys
    nothing, which is why the 12.4MB `by_trip` duplicate is still there and can
    stay. And a per-stop route limit cannot help much, because the rows it drops
    were already accumulated.
    What did help was pooling the two values the scan repeats. Decoding in place
    made one object per row from a tiny set of values: **128,208 `stop_id`
    strings for 583 distinct ones**, plus an int per arrival and departure drawn
    from a few thousand distinct times. A `dict` keyed on the raw bytes took the
    rebuild from 68.2MB to **52.8MB** — 23% — with the cache byte-identical
    (fingerprint over every row of all six tables, unchanged) and the rebuild
    marginally *faster*, a dict hit beating decode-and-parse. `test_the_scan_pools_
    repeated_values` asserts on `id()`, not equality, because equality passes
    whether the pool exists or not.
    What is left is the floor: 128,208 five-tuples cost about 11MB in tuple
    headers and list slots however cheap their contents are. Cutting that needs a
    different data structure (parallel `array`s, say), not a smaller value — and
    at 53MB nothing is asking for it. Beware measuring this with `tracemalloc`:
    its bookkeeping added more than the allocations under study, and an earlier
    figure of 82MB was an artefact of a script that ran the scan twice and held
    both results.

## Measured baselines (Apple silicon unless noted, real data)

Rebuild **2.16s** against the real 89.4MB archive (was 17.29s two-pass), the write
lock held for 0.09s of it — the scan runs before a connection opens, which is why the
board no longer 502s nightly. Scan peak RSS 68MB, up from 62MB: the cost of reading
in 1MB chunks. 4MB chunks measured no faster and 27MB worse. (Both RSS figures are
superseded by the pooling in 29, which was not applied when these were taken.) Cache 7.6MB · duty cycle
<3% at a 15s poll · history 6,018 rows/day = 0.6MB/day, 12MB at 21 days (measured).
Docker image 62.7MB (was 62.4MB before Leaflet's 162KB), non-root, no
pip/fastapi/pydantic. `nix develop` ~10s. **310 tests** collected, 309 passing
and 1 skipped — counted with `pytest`, not remembered; the recorded 216 and 312
were both stale. The 19 in `tests/test_geometry.py` are the newest.
`/api/map` carries 1,904 points in 44KB for a Thursday, against ~450 before the
road geometry landed; the file behind it is 56KB and 2,540 points, fetched once
on load rather than on the 10s poll.

Adding MANGTMGT, measured on x86-64 Linux against the 2026-07-30 archive — three
stops / four unrestricted / four with the 41 limit, same machine, same file, all
after the scan pooling in 29: full rebuild 3.16 / 3.27 / **3.17**s, peak RSS 45.7 /
58.5 / **53.2**MB, cache 7.5 / 18 / **9.1**MB, `load_trips` for a service day 107 /
248 / **142**ms, `segments._scheduled_cells` 225 / 545 / **280**ms. Rebuild time
barely moves in any configuration, because it is a full pass over 4,746,378 rows
either way. The Apple-silicon figures above are not comparable and were left alone.

Peak RSS before the pooling was 52.6 / 74.1 / 68.3MB, so the fourth stop now costs
about 7MB of peak against the three-stop configuration rather than 16MB, and the
whole four-stop rebuild sits below where three stops used to.

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
