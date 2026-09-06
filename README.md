# Swiss Flu Tracker

A small self-contained site showing influenza activity in Switzerland — the current
level, where it sits against the official epidemic threshold, and how the season
compares to the last ten — plus a JSON feed for Home Assistant.

All data comes from the **Federal Office of Public Health (FOPH/BAG)** via the
[Infectious Diseases Dashboard](https://www.idd.bag.admin.ch/diseases/influenza/overview)
open-data API, which is also published on
[opendata.swiss](https://opendata.swiss/en/dataset/influenza1). It updates every
Wednesday.

## Quick start

```sh
python3 fetch.py          # no dependencies, stdlib only
open site/index.html
```

That writes three files into `site/`:

| File | What it is |
|---|---|
| `index.html` | the whole site — data is baked in, so it works over `file://` too |
| `data.json` | everything: full weekly history, per-region and per-canton breakdowns |
| `current.json` | just the current week (< 1 kB) — this is what Home Assistant polls |

## What the numbers mean

There are two different figures on the page, and they are not the same thing.

**Flu-like illness consultations per 100'000** (the big number) comes from the
**Sentinella** network of GPs, extrapolated to the whole population. It is a broad
*activity* signal — people going to the doctor with flu-like symptoms, not confirmed
influenza. This is the figure the FOPH uses to declare a flu epidemic.

**Confirmed cases** are laboratory-confirmed influenza reported under the mandatory
reporting system (published for Switzerland *and* Liechtenstein together, which is
also the only level carrying the influenza A/B split). These are a large undercount
of real infections, since most people with flu are never tested.

### The stages

| Stage | Consultations per 100'000 |
|---|---|
| Baseline | below 20 |
| Low | 20 – 68 |
| **Epidemic** | **68 – 150** |
| High | 150 – 250 |
| Very high | 250 and above |

**Only the 68 / 100'000 epidemic threshold is official.** It is the level at which a
seasonal flu epidemic is declared in Switzerland, and every season since 2013 has
crossed it somewhere between late November and early January. The other cut-points
are derived from the distribution of weekly national incidence since 2013 (roughly
the 90th and 95th percentiles) so that the single threshold has some shape above and
below it. They are a reading aid, not policy levels. Change them at the top of
[`fetch.py`](fetch.py) if you prefer different bands.

## Home Assistant

Home Assistant polls `current.json` over HTTP, so the file needs to be reachable
from your HA instance. Two ways:

### A. Publish with GitHub Pages (recommended)

The included workflow rebuilds every Wednesday and deploys to Pages:

```sh
git init && git add . && git commit -m "Swiss flu tracker"
gh repo create flu-tracker --public --source=. --push
```

Then in the repo: **Settings → Pages → Build and deployment → Source: GitHub Actions**.
The site lands at `https://<user>.github.io/flu-tracker/` and the feed at
`https://<user>.github.io/flu-tracker/current.json`.

Copy [`homeassistant/rest_sensors.yaml`](homeassistant/rest_sensors.yaml) into your
`configuration.yaml`, replacing the resource URL with yours.

### B. Serve it from the Home Assistant box

Drop `fetch.py` and `template.html` somewhere HA can reach, write into
`/config/www/flu/` (served at `/local/flu/`), and run it from cron:

```sh
30 13 * * 3  /usr/bin/python3 /config/flu-tracker/fetch.py --out /config/www/flu
```

Then point the REST sensor at `http://127.0.0.1:8123/local/flu/current.json`.

If you would rather skip HTTP entirely, `fetch.py --print` writes the current status
to stdout as one line of JSON, which suits a `command_line` sensor:

```yaml
command_line:
  - sensor:
      name: "Flu Switzerland activity"
      command: "python3 /config/flu-tracker/fetch.py --print"
      value_template: "{{ value_json.ili_incidence }}"
      json_attributes: [week, stage, stage_key, trend, epidemic, confirmed_cases]
      scan_interval: 10800
```

### What you get

| Entity | State |
|---|---|
| `sensor.flu_switzerland_activity` | consultations per 100'000 (all other fields are attributes) |
| `sensor.flu_switzerland_stage` | `Baseline` / `Low` / `Epidemic` / `High` / `Very high` |
| `sensor.flu_switzerland_confirmed_cases` | lab-confirmed cases this week |
| `sensor.flu_switzerland_positivity` | % of Sentinella swabs positive for flu |
| `binary_sensor.flu_ch_epidemic` | on when above the epidemic threshold |

[`homeassistant/automations.yaml`](homeassistant/automations.yaml) has notifications
for crossing the epidemic threshold and for any stage change;
[`homeassistant/lovelace-card.yaml`](homeassistant/lovelace-card.yaml) is a dashboard
card.

## Data sources

| Dataset | Used for |
|---|---|
| `INFLUENZA_sentinella` | ILI consultations per 100'000, national + 6 Sentinella regions |
| `INFLUENZA_oblig` | lab-confirmed cases, by influenza type and by canton |
| `RESPVIRUSES_sentinella` | share of Sentinella swabs testing positive for influenza |

All at `https://idd.bag.admin.ch/api/v1/export/latest/<dataset>/csv`. Licence:
opendata.swiss free use, source must be acknowledged.
