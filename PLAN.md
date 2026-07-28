# PLAN

## Done

- [x] Feasibility confirmed against the live feed with a real key
- [x] Stops resolved from NaPTAN area 180 to ATCO codes
- [x] `.gitignore`, `.env` at mode 600, redaction helper
- [x] SQLite schema: timetable cache plus observation history
- [x] GTFS ingest, streaming the 398MB `stop_times.txt` in two passes
- [x] SIRI-VM fetch and parse, with staleness filtering
- [x] Tiered vehicle-to-trip matcher working around the broken journey-code join
- [x] Observation history, stop-event derivation, segment learning
- [x] ETA prediction with learned medians and timetable fallback
- [x] FastAPI poller, JSON API, `/healthz`, HTML dashboard
- [x] Nix flake and direnv
- [x] Multi-stage Dockerfile and compose, sized for `tailscale serve`
- [x] README, project memory

- [x] Unit and end-to-end tests: 102 passing, 91% coverage
- [x] `ruff format`, `ruff check`, `mypy ontime` clean, in the venv and under nix
- [x] `nix develop` verified: 12s after cutting the scipy build out of the closure
- [x] Docker image verified: 220MB, non-root, no pip or dev tooling
- [x] Container run against the live feed with a real key
- [x] Profiled with `scripts/benchmark.py`; removed four dead indices
- [x] Timestamped logging (ISO-8601 UTC) with key redaction as a handler filter
- [x] Image 220MB to 62.4MB: alpine base, fastapi dropped for starlette, pruning
      moved into the builder so deletions actually reclaim space

- [x] Guard against a second writer (`ontime/locking.py`) after a host ingest
      SIGBUSed against a leftover bind-mounted container
- [x] Direction filter: wrong-way buses were being matched and shown, producing
      delays up to 362 minutes. Live delay spread now -1 to +7 min, all tier 1

- [x] Profiled the hot paths again and acted on all five findings: dropped the four
      stale indices from existing databases (removing them from the schema had never
      removed them from the volume), bounded `stop_events` at 90 days so the hourly
      relearn stops growing, memoised `_nearest` and `dep_delta` inside `match`, gave
      `scheduled_only` a precomputed watched-stop list, and carried the matched
      position into `eta.predict`. Rebuild scan 17.29s to 2.16s on a single byte-level
      pass over `stop_times.txt`, output verified identical table by table.

- [x] `/segments`, a page for judging the learned model rather than running it:
      the observed → stored → used → significant funnel, coverage against the
      6,771 segment-buckets the timetable implies, a sample-count histogram with
      both gates drawn on it, and every segment with a distribution-free
      confidence interval on its median. Showed three things the stats table
      cannot express — coverage is 3.0%, nothing yet clears n = 6, and 267 of
      484 learned buckets pair two stops the timetable never runs consecutively,
      so the predictor has no key that could ever reach them.

## In progress

- [ ] Nothing. Waiting on history to accumulate.

- [x] Traced the wasted learning to its cause and fixed it. `learn_segments`
      paired consecutive *detections* rather than consecutive *stops*, so a
      missed stop was silently absorbed into a segment spanning it: 263 of 484
      buckets, all unreachable by `eta.predict`. Pairing now requires scheduled
      adjacency. The guess that `STOP_RADIUS_M` was to blame was wrong and the
      measurement says so — detections sit a median 29m from the stop against
      1,019m for the misses, and runs watched continuously miss nothing at all.

## Next

- [ ] Measure the skipped share on the deployment, where the poller runs
      continuously. The local figure (57%) comes from ad-hoc dev runs and says
      nothing about production. If it stays high there, the next suspects are
      vehicles leaving `BBOX` mid-journey and operator dropouts, neither of
      which a wider radius would help.
- [ ] Reconsider `MIN_SAMPLES = 5`. It sits one observation below the smallest
      sample that can carry a 95% interval; 6 would cost little and mean the
      threshold matched a claim that can be made.
- [ ] Run for a fortnight, then read the Δ column on `/segments` to see which
      segments the schedule gets wrong
- [ ] Interpolate along the shape rather than snapping to the nearest stop —
      `shapes.txt` is in the archive and currently unused
- [ ] Confidence intervals on the board using the stored `p85_secs`
- [ ] Cross-check a sample of predictions against bustimes.org to quantify error

## Deliberately not doing

- Commercial SIRI-SM subscription. The learned model should get close enough, and
  the whole point is staying on open data.
- Authentication. `tailscale serve` on a private tailnet is the security boundary.
