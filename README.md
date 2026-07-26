# ontime

A private live-departures dashboard for three bus stops in Manchester, built on the Department for Transport (DfT) *Bus Open Data Service* (BODS).

The stops watched by default:

| NaPTAN | ATCO | Stop | Services |
|---|---|---|---|
| `MANADGMT` | `1800EB01881` | Hyde Grove, Plymouth Grove (westbound) | 50, 197, 53, 41, 191, 51 |
| `MANGPWTD` | `1800SB13961` | Swinton Grove, Upper Brook Street (Stop L) | 50, 197, 53, 41, 191, 51 |
| `MANADTDW` | `1800EB06241` | Cavanagh Close, Stockport Road (northwest-bound) | 192 |

Together these see 1,135 scheduled calls per day, of which service 192 alone accounts for 762.

## Why arrival times are computed here

BODS publishes vehicle positions in *Standard Interface for Real-time Information* (SIRI) format, specifically the Vehicle Monitoring profile, SIRI-VM. The standard permits a `MonitoredCall` element carrying an `ExpectedArrivalTime` for each stop ahead. Greater Manchester publishers do not populate it. A sample of 417 vehicles across the conurbation contained no `MonitoredCall`, no `OnwardCalls` and no `ExpectedArrivalTime` of any kind, and the *General Transit Feed Specification – Realtime* (GTFS-RT) mirror exposes only `VehiclePositions` with no `TripUpdates`.

The feed therefore answers *where is the bus* and never *when will it reach my stop*. Everything on this dashboard beyond a map pin is derived locally, by matching each vehicle to a timetabled trip and walking the remaining stop sequence.

## How prediction works

**Match.** Each live vehicle is tied to a timetabled trip. The obvious key does not work: BODS ships a `vehicle_journey_code` in its converted GTFS and a `DatedVehicleJourneyRef` in SIRI-VM, and the converter regenerates the former rather than preserving the operator's. Measured against the services calling at these stops, service 50 matched 0 of 16 live vehicles. What survives conversion is the shape of the journey, so matching keys on origin stop, destination stop and aimed departure time, loosening through three tiers until something fits.

**Locate.** The closest stop in the matched trip's sequence gives the vehicle's progress, and its distance along the current leg prorates the partial segment.

**Sum.** Remaining segments are added up. Because the sum starts from where the bus is rather than where it should be, lateness needs no separate correction — a bus twenty minutes behind is simply twenty minutes further back in the sequence.

**Learn.** Every polled position is stored. Once a run goes quiet, positions reduce to one closest-approach event per stop, and those events aggregate into median traversal times per segment, per hour, per day type. Segments with at least 5 samples use the observed median; everything else falls back to the timetabled gap. Rows on the dashboard are tagged so the two are distinguishable.

Raw positions are trimmed to `ONTIME_RETAIN_DAYS` (21 by default). The aggregates derived from them are kept permanently, so accuracy improves while the database stays bounded.

## Running it

### Development

The flake pins the whole toolchain, so `direnv allow` is the only setup step:

```sh
cp .env.example .env      # add the BODS key
chmod 600 .env
direnv allow              # or: nix develop

python -m ontime.ingest   # build the timetable cache, roughly 4 minutes
python -m ontime.web      # http://127.0.0.1:8000
```

Registration for a free key takes a minute at [data.bus-data.dft.gov.uk](https://data.bus-data.dft.gov.uk/account/signup/).

### Docker, behind Tailscale

The compose file runs two services over one named volume. The dashboard polls and predicts; the maintenance loop refreshes the 89MB timetable archive daily and relearns segments hourly, so a download never stalls the departure board.

```sh
docker compose up -d --build
```

The published port binds to `127.0.0.1` deliberately — nothing reaches the local network. Putting it on the tailnet is a separate, explicit step:

```sh
tailscale serve --bg 8000
tailscale serve status
```

That gives an HTTPS URL on the tailnet with a certificate issued automatically, reachable only by devices signed into the same account. To withdraw it, `tailscale serve --https=443 off`. Avoid `tailscale funnel` here: it publishes to the open internet, and this application has no authentication of its own.

## Configuration

Every setting is an environment variable, listed in `.env.example`.

| Variable | Default | Meaning |
|---|---|---|
| `BODS_API_KEY` | — | Required. Never committed, never sent to the browser. |
| `ONTIME_BBOX` | `-2.32,53.38,-2.10,53.52` | Feed bounding box, `min_lon,min_lat,max_lon,max_lat`. |
| `ONTIME_POLL_SECS` | `15` | Feed poll interval. The upstream refreshes every 10 seconds. |
| `ONTIME_STALE_SECS` | `180` | Discard positions older than this. |
| `ONTIME_HORIZON_SECS` | `3600` | How far ahead the board looks. |
| `ONTIME_RETAIN_DAYS` | `21` | Raw position retention. Aggregates outlive it. |
| `ONTIME_DATA_DIR` | `./data` | Cache location, set to `/data` in the container. |

Changing the watched stops means editing `STOPS` in `ontime/config.py` and re-running the ingest.

## Keeping the key safe

The key is low-value — it grants read access to public data with no billing attached — so the realistic risk is someone else exhausting the rate limit against the account rather than damage. The measures here are proportionate to that.

The credential lives only in `.env`, which is gitignored, mode `600`, and excluded from the Docker build context. It is read into the server process and never reaches the browser, because the page calls this application's own endpoints rather than BODS directly. `config.redact()` strips it from anything heading for a log, which matters more than it sounds: `requests` puts the full URL, query string included, into exception messages, and that is the most likely way for a key to end up in a log file. The fixture builder asserts the key never appears in a captured sample.

If it does leak, rotate it in the BODS account settings; there is no way to scope or restrict an existing key.

## Testing

```sh
pytest -q                     # unit and end-to-end
pytest -q -m e2e              # end-to-end only
pytest -q -m live             # hits the real feed, needs a key
ruff check . && ruff format --check .
mypy ontime
```

Fixtures are cut from real published data rather than invented — `tests/fixtures/mini_gtfs.zip` is a 30-trip subset of the BODS North West archive, and `tests/fixtures/siri_sample.xml` is a recorded live response. `scripts/build_fixtures.py` regenerates both. Calendar rows in the subset are normalised to run every day between 2020 and 2035 so the suite does not expire.

## Layout

```
ontime/config.py       settings, stop definitions, redaction
ontime/db.py           SQLite schema
ontime/ingest.py       GTFS download and cache build
ontime/siri.py         feed fetch and parse
ontime/matching.py     vehicle to trip matching
ontime/history.py      observation storage and segment learning
ontime/eta.py          arrival prediction
ontime/web.py          poller and HTTP API
ontime/maintenance.py  periodic refresh loop
```

Accuracy on a straight corridor such as Upper Brook Street settles within a minute or two once a fortnight of history has accumulated, and degrades in the way anyone who has waited on Stockport Road would expect. A commercial Stop Monitoring feed with a real prediction engine would beat it, though not by as much as the price difference suggests.
