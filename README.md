# ontime

A live-departures dashboard for four bus stops in Manchester, built on the Department for Transport (DfT) *Bus Open Data Service* (BODS).

BODS publishes vehicle positions in the *Standard Interface for Real-time Information* Vehicle Monitoring profile (SIRI-VM). The standard permits an `ExpectedArrivalTime` per stop ahead; Greater Manchester publishers do not populate it. A sample of 417 vehicles contained no `MonitoredCall`, no `OnwardCalls` and no expected arrival of any kind, and the *General Transit Feed Specification – Realtime* (GTFS-RT) mirror carries positions without trip updates.

The feed answers *where is the bus* and never *when will it reach my stop*. Every arrival time here is computed locally: each vehicle is matched to a timetabled trip, located in that trip's stop sequence, and the remaining segments are summed. Observed traversal times accumulate over time and replace the timetabled gaps once a segment has enough samples.

## The map

Below the departure boards sits a map of the same data: a pin for each watched stop, and a dot for every vehicle the matcher has placed on a trip, rotated to the bearing the feed reports. Route lines run through each route's stop sequence rather than along the road, because every one of the 1,261 watched trips carries an empty `shape_id` and there is no published geometry to draw. Stop spacing has a median of 284m, so the line follows a straight road closely and cuts the corner at a bend.

One line is drawn per route, from the longest variant any of its trips runs, which is exact for seven of the nine routes cached. It is not exact for the 41, whose Oxford Road and Swinton Grove workings share a number and little else, so its line misses ten of the stops its shorter variants call at. The line is context beneath the pins rather than a route diagram, so it is left as it is.

The basemap is the one part of this application that talks to a third party. Tiles load from the server named in `ONTIME_MAP_TILE_URL`, and the page's *Content Security Policy* permits that origin and no other. Leaflet 1.9.4 is vendored under `ontime/static/vendor/` rather than loaded from a content delivery network, which costs 162KB in the image and removes a remote dependency from a dashboard that has to work when something else is down.

## What the model knows

Learned traversal times are the part of this project that improves with age, and the part hardest to inspect. `/segments` reports the state of them: how many of the *segments* the timetable implies have been observed at all, how many carry the five samples `eta.predict` requires, and how wide the uncertainty on each median is. It rebuilds the sample vectors the learner was fitted to rather than reading the summary rows, so the page and the running model cannot drift apart.

That denominator is not a constant, and it moved when the Oxford Road stop was added: a weekday cache implied 6,736 segments at three stops and 9,199 at four. Coverage as a percentage therefore fell by about a third overnight without a single observation being lost. A drop across that boundary reads as a bigger timetable, not as a regression.

Intervals are *distribution-free*, taken from order statistics instead of an assumed shape, because traversal times are bounded below by the road and not bounded above by traffic. The widest interval n samples can offer covers the true median with probability 1 − 2/2ⁿ, which first clears 95% at n = 6 — one more than the gate requires. The page also counts the hops it could not measure because a stop between two detections went unseen, a gap in watching rather than in the timetable.

The page is deliberately unflattering. A dashboard reporting how much it has learned should make the shortfall the easiest thing on it to read.

## Running it

The Nix flake pins the toolchain, so `direnv allow` is the only setup step. A free key takes a minute to register at [data.bus-data.dft.gov.uk](https://data.bus-data.dft.gov.uk/account/signup/).

```sh
cp .env.example .env      # add the BODS key
chmod 600 .env
direnv allow              # or: nix develop

python -m ontime.ingest   # build the timetable cache, roughly 4 minutes
python -m ontime.web      # http://127.0.0.1:8000
```

Under Docker, the compose file runs the dashboard and a maintenance loop over one named volume, so refreshing the 89MB timetable archive never stalls the departure board:

```sh
docker compose up -d --build
```

The published port binds to `127.0.0.1`. Exposing it is a separate step — `tailscale serve --bg 8000` gives an HTTPS URL reachable only by devices on the same tailnet. Avoid `tailscale funnel`: it publishes to the open internet and this application has no authentication of its own.

## Configuration

Settings are environment variables, listed with their defaults in `.env.example`. Only `BODS_API_KEY` is required; it stays in `.env` and never reaches the browser, because the page calls this application's own endpoints rather than BODS directly.

Changing the watched stops means editing `STOPS` in `ontime/config.py` and re-running the ingest.

## Testing

```sh
pytest -q                     # unit and end-to-end
pytest -q -m live             # hits the real feed, needs a key
ruff check . && ruff format --check .
mypy ontime
```

Fixtures are cut from real published data rather than invented: `tests/fixtures/mini_gtfs.zip` is a 30-trip subset of the BODS North West archive and `tests/fixtures/siri_sample.xml` is a recorded response. `scripts/build_fixtures.py` regenerates both.

## Layout

```
ontime/config.py       settings, stop definitions, redaction
ontime/logs.py         timestamped logging and the redaction filter
ontime/db.py           SQLite schema
ontime/ingest.py       GTFS download and cache build
ontime/siri.py         feed fetch and parse
ontime/matching.py     vehicle to trip matching
ontime/history.py      observation storage and segment learning
ontime/segments.py     what the learned model knows, and how firmly
ontime/eta.py          arrival prediction
ontime/web.py          poller and HTTP API
ontime/static/         the dashboard pages and vendored Leaflet
ontime/maintenance.py  periodic refresh loop
```

Accuracy on a straight corridor settles within a minute or two once a fortnight of history has accumulated, and degrades in the way anyone who has waited on Stockport Road would expect. A commercial feed with a real prediction engine would beat it, though not by as much as the price difference suggests.
