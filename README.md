# Swiss Flu Tracker

A small self-contained site showing influenza activity in Switzerland — the current
level, where it sits against the official epidemic threshold, and how the season
compares to the last ten — plus a JSON feed for Home Assistant.

Influenza is the headline, but the Sentinella lab tests every swab for **RSV** and
**SARS-CoV-2** on the same PCR panel and reports all three in one file, so the
tracker carries all three. That costs no extra request and gives the page something
to say in July, when flu is at background level.

The same three viruses are also measured **daily in wastewater** at eleven treatment
plants. That signal does not depend on anyone visiting a doctor, and it usually runs
a few days ahead of the weekly clinical figures.

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

| File           | What it is                                                            |
| -------------- | --------------------------------------------------------------------- |
| `index.html`   | the whole site — data is baked in, so it works over `file://` too     |
| `data.json`    | everything: weekly history, the PCR panel, region/canton/age breakdowns |
| `current.json` | just the latest figures (~1.1 kB) — this is what Home Assistant polls |

## What the numbers mean

There are four different figures on the page, and they are not the same thing.

**Flu-like illness consultations per 100'000** (the big number) comes from the
**Sentinella** network of GPs, extrapolated to the whole population. It is a broad
*activity* signal — people going to the doctor with flu-like symptoms, not confirmed
influenza. This is the figure the FOPH uses to declare a flu epidemic.

**Confirmed cases** are laboratory-confirmed influenza reported under the mandatory
reporting system (published for Switzerland *and* Liechtenstein together, which is
also the only level carrying the influenza A/B split). These are a large undercount
of real infections, since most people with flu are never tested.

**Swab positivity** is a third thing again: the share of Sentinella PCR swabs testing
positive, reported separately for influenza, RSV and SARS-CoV-2. One swab is tested
for all three, so the three rates share a denominator and can sum above 100% — a swab
can be positive for more than one virus. Read it as *what is circulating among people
who saw a doctor and got swabbed*, not as prevalence in the population.

> **Watch the denominator.** In peak season this rests on 100–150 swabs a week; in
> summer it can be fewer than ten, and one detection then moves the rate by tens of
> points. `samples_tested` is published alongside every rate, the page prints the
> season's swab count under the chart, and Home Assistant gets it as
> `sensor.sentinella_swabs_tested`. Gate any automation on it.

**Wastewater viral load** is the only figure here that does not count patients. It is
gene copies per person per day, measured in the sewage of eleven treatment plants
serving about 2 million of Switzerland's 9 million people. It is immune to whether
people seek care or get tested, and it is daily rather than weekly.

> **Never compare the three wastewater panels against each other.** How much virus an
> infected person sheds differs enormously by virus, so SARS-CoV-2 load sitting an
> order of magnitude above influenza says nothing about how many people have either.
> Each virus is only meaningful against its own history, which is why the page gives
> each one its own vertical scale.

### The stages

| Stage        | Consultations per 100'000 |
| ------------ | ------------------------- |
| Baseline     | below 20                  |
| Low          | 20 – 68                   |
| **Epidemic** | **68 – 150**              |
| High         | 150 – 250                 |
| Very high    | 250 and above             |

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
gh repo create swiss-virus-tracker --public --source=. --push
```

Then in the repo: **Settings → Pages → Build and deployment → Source: GitHub Actions**.
The site lands at `https://<user>.github.io/swiss-virus-tracker/` and the feed at
`https://<user>.github.io/swiss-virus-tracker/current.json`.

Copy [`homeassistant/flu_tracker.yaml`](homeassistant/flu_tracker.yaml) into your
`packages/` directory, replacing the resource URL with yours. It is a single package
holding the sensors, the binary sensor and the automations.

Note that GitHub disables scheduled workflows after 60 days of repository inactivity,
so the Wednesday rebuild will stop on its own eventually.
[`MAINTENANCE.md`](MAINTENANCE.md) covers that and the other ways this quietly stops
working.

### B. Serve it from the Home Assistant box

Drop `fetch.py` and `template.html` somewhere HA can reach, write into
`/config/www/flu/` (served at `/local/flu/`), and run it from cron:

```sh
30 13 * * 3  /usr/bin/python3 /config/swiss-virus-tracker/fetch.py --out /config/www/flu
```

Then point the REST sensor at `http://127.0.0.1:8123/local/flu/current.json`.

If you would rather skip HTTP entirely, `fetch.py --print` writes the current status
to stdout as one line of JSON, which suits a `command_line` sensor:

```yaml
command_line:
  - sensor:
      name: "Flu Switzerland activity"
      command: "python3 /config/swiss-virus-tracker/fetch.py --print"
      value_template: "{{ value_json.ili_incidence }}"
      json_attributes: [week, stage, stage_key, trend, epidemic, confirmed_cases]
      scan_interval: 10800
```

### What you get

| Entity                                   | State                                                       |
| ---------------------------------------- | ----------------------------------------------------------- |
| `sensor.flu_switzerland_activity`        | consultations per 100'000 (all other fields are attributes) |
| `sensor.flu_switzerland_stage`           | `Baseline` / `Low` / `Epidemic` / `High` / `Very high`      |
| `sensor.flu_switzerland_confirmed_cases` | lab-confirmed cases this week                               |
| `sensor.flu_switzerland_positivity`      | % of Sentinella swabs positive for flu                      |
| `sensor.rsv_switzerland_positivity`      | % of the same swabs positive for RSV                        |
| `sensor.covid_switzerland_positivity`    | % of the same swabs positive for SARS-CoV-2                 |
| `sensor.sentinella_swabs_tested`         | swabs behind those three rates this week                    |
| `sensor.flu_switzerland_wastewater`      | flu in wastewater, million copies/person/day (daily)        |
| `sensor.rsv_switzerland_wastewater`      | RSV in wastewater, same unit                                |
| `sensor.covid_switzerland_wastewater`    | SARS-CoV-2 in wastewater, same unit                         |
| `binary_sensor.flu_ch_epidemic`          | on when above the epidemic threshold                        |

The same package has notifications for crossing the epidemic threshold and for any
stage change; [`homeassistant/lovelace-card.yaml`](homeassistant/lovelace-card.yaml)
is a dashboard card.

## Data sources

| Dataset                  | Used for                                                                    |
| ------------------------ | --------------------------------------------------------------------------- |
| `INFLUENZA_sentinella`   | ILI consultations per 100'000: national, 6 Sentinella regions, 5 age bands  |
| `INFLUENZA_oblig`        | lab-confirmed cases by influenza type, canton and age; the FOPH's 10-y band |
| `RESPVIRUSES_sentinella` | swab positivity for influenza / RSV / SARS-CoV-2, plus influenza subtypes   |
| `RESPVIRUSES_wastewater` | daily viral load for the same three, at 11 treatment plants                 |

Four requests. The wastewater export is ~12 MB and dominates the runtime — a build
takes roughly ten seconds, almost all of it that one download.

**The wastewater export has no national figure**, only individual plants, so
`fetch.py` builds one. That is less trivial than it sounds: plants sample on
different days, so between one and eleven report on any given date, and they serve
populations from 39k to 471k. Averaging raw daily values tracks *which* plants
reported far more than it tracks the virus — 58% mean day-over-day swing, against 9%
using the per-plant 7-day means the FOPH publishes alongside. So the script sums each
day's per-plant 7-day means and divides by the population those plants actually
serve, and drops any day where the reporting plants cover less than half the
monitored population. Without that last rule the tail of the series is a single
plant swinging the national figure by a factor of three. See `WW_MIN_COVERAGE`.

`COVID19_oblig` and `COVID19_sentinella` exist and were left out — they would each
need their own request and their own page section. There is no dataset index endpoint
on this API; names are found by probing, and most guesses 404.

All at `https://idd.bag.admin.ch/api/v1/export/latest/<dataset>/csv`. Licence:
opendata.swiss free use, source must be acknowledged.
