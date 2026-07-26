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

## In progress

- [ ] Unit tests: siri parsing, service-day maths, matcher tiers, ETA arithmetic,
      segment learning, redaction
- [ ] End-to-end tests: ingest the mini GTFS fixture, replay the recorded SIRI
      sample through the poller, assert on the rendered board and the HTTP API
- [ ] `ruff format`, `ruff check`, `mypy ontime` clean
- [ ] Verify `nix develop` and `nix flake check`
- [ ] Verify the Docker image builds and contains no dev tooling

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
