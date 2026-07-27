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

## In progress

- [ ] Nothing. Waiting on history to accumulate.

## Next

- [ ] Run for a fortnight, then compare learned medians against the timetable to
      see which segments the schedule gets wrong
- [ ] Interpolate along the shape rather than snapping to the nearest stop —
      `shapes.txt` is in the archive and currently unused
- [ ] Confidence intervals on the board using the stored `p85_secs`
- [ ] Cross-check a sample of predictions against bustimes.org to quantify error

## Deliberately not doing

- Commercial SIRI-SM subscription. The learned model should get close enough, and
  the whole point is staying on open data.
- Authentication. `tailscale serve` on a private tailnet is the security boundary.
