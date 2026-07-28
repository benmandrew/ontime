# ontime

A live-departures dashboard for three bus stops in Manchester, built on the Department for Transport (DfT) *Bus Open Data Service* (BODS).

BODS publishes vehicle positions in the *Standard Interface for Real-time Information* Vehicle Monitoring profile (SIRI-VM). The standard permits an `ExpectedArrivalTime` per stop ahead; Greater Manchester publishers do not populate it. A sample of 417 vehicles contained no `MonitoredCall`, no `OnwardCalls` and no expected arrival of any kind, and the *General Transit Feed Specification – Realtime* (GTFS-RT) mirror carries positions without trip updates.

The feed answers *where is the bus* and never *when will it reach my stop*. Every arrival time here is computed locally: each vehicle is matched to a timetabled trip, located in that trip's stop sequence, and the remaining segments are summed. Observed traversal times accumulate over time and replace the timetabled gaps once a segment has enough samples.

## The map

Below the departure boards sits a map of the same data: a pin for each watched stop, and a dot for every vehicle the matcher has placed on a trip, rotated to the bearing the feed reports. Pins and dots differ in shape rather than only in colour, because the *OpenStreetMap* basemap underneath has already spent most of the palette on its own roads.

Route lines are drawn through each route's stop sequence. That is not the geometry the operators intended, and the reason is worth stating: GTFS ships road shapes in `shapes.txt`, 131MB unpacked, but every one of the 1,135 watched trips carries an empty `shape_id`, as do 66,967 of the feed's 106,058 trips. There is no road geometry to draw. Stop spacing has a median of 284m, so a line through consecutive stops follows a straight road closely and cuts the corner at a bend.

The basemap is the one part of this application that talks to a third party. Tiles load directly from the tile server named in `ONTIME_MAP_TILE_URL`, and the page's *Content Security Policy* (CSP) permits that origin and no other; `web.tile_origin` derives the policy from the same setting, so the two cannot drift apart. Leaflet 1.9.4 is vendored under `ontime/static/vendor/` rather than loaded from a content delivery network, which costs 162KB in the image and removes a remote dependency from a dashboard that has to work when something else is down.

## Which segments are learned

Learned traversal times are the part of this project that improves with age, and the part hardest to inspect. `segment_stats` holds three numbers per *segment* — a median, an 85th percentile, and a sample count — keyed on the route, the two stops, the local hour, and whether the day falls at a weekend. `eta.predict` consults a row only once it holds five samples; below that the timetabled gap stands in. But three numbers cannot say whether five samples are enough, and the threshold is the whole of the model's notion of confidence.

`/segments` answers that question separately. It rebuilds the sample vectors the learner was fitted to, rather than reading the summary rows, so the page and the running model cannot drift apart.

Uncertainty on each median is reported as a *distribution-free* interval, taken from order statistics instead of an assumed shape. Traversal times are not normal: the road bounds them below and traffic does not bound them above. The widest interval n samples can offer is the whole observed range, which covers the true median with probability 1 − 2/2ⁿ. That first clears 95% at n = 6. A segment sitting exactly on the five-sample threshold therefore has no 95% interval at any width — the gate is one observation short of the smallest sample that could support the claim.

Two further counts come from comparing the learned buckets against the timetable rather than against each other. Coverage measures the segments observed at least once against every segment the timetable implies, which is 6,771 buckets across the seven watched routes.

The second count came out of a defect the page found on its first run. A run holds only the stops that were detected, so when one goes unobserved the events either side of it sit next to each other in the list while being two stops apart on the road. `learn_segments` paired them regardless, recording a traversal across the missed stop — a bucket keyed on two stops no trip runs consecutively, which `eta.predict` then never looks up, because it builds its keys by walking the scheduled sequence. On one day of real data that was 263 of 484 buckets. The duration guard was meant to catch exactly this, and cannot: a skipped hop between two closely spaced stops lands comfortably inside the five-second to thirty-minute window. Pairing now requires the two events to be adjacent in the trip's stop sequence, which removed all 263 and changed the samples of no bucket the predictor reads.

What remains is the honest version of the same measurement, and the page reports it as *skipped*: hops that could not be measured because a stop between two detections went unseen. That is a gap in watching rather than in the timetable, and it is the one input to the model nothing else on the page describes.

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
