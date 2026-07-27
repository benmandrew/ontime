# ontime

A live-departures dashboard for three bus stops in Manchester, built on the Department for Transport (DfT) *Bus Open Data Service* (BODS).

BODS publishes vehicle positions in the *Standard Interface for Real-time Information* Vehicle Monitoring profile (SIRI-VM). The standard permits an `ExpectedArrivalTime` per stop ahead; Greater Manchester publishers do not populate it. A sample of 417 vehicles contained no `MonitoredCall`, no `OnwardCalls` and no expected arrival of any kind, and the *General Transit Feed Specification – Realtime* (GTFS-RT) mirror carries positions without trip updates.

The feed answers *where is the bus* and never *when will it reach my stop*. Every arrival time here is computed locally: each vehicle is matched to a timetabled trip, located in that trip's stop sequence, and the remaining segments are summed. Observed traversal times accumulate over time and replace the timetabled gaps once a segment has enough samples.

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
ontime/eta.py          arrival prediction
ontime/web.py          poller and HTTP API
ontime/maintenance.py  periodic refresh loop
```

Accuracy on a straight corridor settles within a minute or two once a fortnight of history has accumulated, and degrades in the way anyone who has waited on Stockport Road would expect. A commercial feed with a real prediction engine would beat it, though not by as much as the price difference suggests.
