#!/usr/bin/env python3
"""Pull Swiss influenza surveillance data from the FOPH (BAG) Infectious
Diseases Dashboard open-data API and render a static site + JSON feed.

Data source: https://idd.bag.admin.ch/api/v1/export/latest/<dataset>/csv
Published by the Federal Office of Public Health, also listed on opendata.swiss.
Updated every Wednesday.

Usage:
    python3 fetch.py                 # write site/data.json and site/index.html
    python3 fetch.py --print         # print the "current" block only (Home Assistant)
    python3 fetch.py --out DIR       # write somewhere other than ./site
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import statistics
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

API = "https://idd.bag.admin.ch/api/v1/export/latest/{dataset}/csv"

DATASETS = {
    # ILI consultations reported by the Sentinella GP network -> the metric the
    # FOPH uses to declare a flu epidemic (incidence per 100'000 inhabitants).
    "sentinella": "INFLUENZA_sentinella",
    # Laboratory-confirmed influenza, mandatory reporting -> absolute case counts.
    "oblig": "INFLUENZA_oblig",
    # Sentinella swabs tested by PCR -> share of samples positive for influenza.
    "respviruses": "RESPVIRUSES_sentinella",
}

# The 68 / 100'000 epidemic threshold is the FOPH's official definition.
# The other cut-points are derived from the distribution of weekly national
# incidence since 2013 (~p90 and ~p95) to give the bare threshold some shape.
EPIDEMIC_THRESHOLD = 68.0

# How many completed seasons form the min/median/max reference band on the chart.
REFERENCE_SEASONS = 10

# Confirmed cases are published for Switzerland + Liechtenstein ("CHFL"); that is
# the only geography carrying the influenza A / B breakdown.
CASES_REGION = "CHFL"

STAGES = [
    # (lower bound, key, label, short description)
    (250.0, "very_high", "Very high", "Among the most intense weeks on record."),
    (150.0, "high", "High", "Well above the epidemic threshold; peak-season levels."),
    (EPIDEMIC_THRESHOLD, "epidemic", "Epidemic", "Above the FOPH epidemic threshold of 68 / 100'000."),
    (20.0, "low", "Low", "Flu is circulating but below the epidemic threshold."),
    (0.0, "baseline", "Baseline", "Off-season background level."),
]

SENTINELLA_REGIONS = {
    "region_1": ("Region 1", "GE, NE, VD, VS"),
    "region_2": ("Region 2", "BE, FR, JU"),
    "region_3": ("Region 3", "AG, BL, BS, SO"),
    "region_4": ("Region 4", "LU, NW, OW, SZ, UR, ZG"),
    "region_5": ("Region 5", "AI, AR, GL, SG, SH, TG, ZH"),
    "region_6": ("Region 6", "GR, TI"),
}

CANTONS = {
    "AG": "Aargau", "AI": "Appenzell Innerrhoden", "AR": "Appenzell Ausserrhoden",
    "BE": "Bern", "BL": "Basel-Landschaft", "BS": "Basel-Stadt", "FR": "Fribourg",
    "GE": "Genève", "GL": "Glarus", "GR": "Graubünden", "JU": "Jura", "LU": "Luzern",
    "NE": "Neuchâtel", "NW": "Nidwalden", "OW": "Obwalden", "SG": "St. Gallen",
    "SH": "Schaffhausen", "SO": "Solothurn", "SZ": "Schwyz", "TG": "Thurgau",
    "TI": "Ticino", "UR": "Uri", "VD": "Vaud", "VS": "Valais", "ZG": "Zug",
    "ZH": "Zürich", "FL": "Liechtenstein",
}


def fetch(dataset: str) -> list[dict]:
    url = API.format(dataset=dataset)
    req = urllib.request.Request(url, headers={"User-Agent": "flu-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        text = resp.read().decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def num(value: str | None):
    """CSV uses the string 'NA' for missing values."""
    if value is None or value in ("NA", "", "NULL"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def week_start(week: str) -> str:
    year, w = week.split("-W")
    return dt.date.fromisocalendar(int(year), int(w), 1).isoformat()


def season_of(week: str) -> str:
    """Flu seasons run W40 -> W39 of the following year."""
    year, w = week.split("-W")
    year, w = int(year), int(w)
    start = year if w >= 40 else year - 1
    return f"{start}/{start + 1}"


def stage_for(incidence: float | None) -> dict:
    if incidence is None:
        return {"key": "unknown", "label": "Unknown", "level": -1,
                "description": "No incidence reported for this week."}
    for idx, (lower, key, label, desc) in enumerate(STAGES):
        if incidence >= lower:
            return {"key": key, "label": label, "level": len(STAGES) - 1 - idx,
                    "description": desc}
    return {"key": "baseline", "label": "Baseline", "level": 0, "description": ""}


def pick(rows, **filters):
    for row in rows:
        if all(row.get(k) == v for k, v in filters.items()):
            yield row


def build() -> dict:
    sent = fetch(DATASETS["sentinella"])
    oblig = fetch(DATASETS["oblig"])
    resp = fetch(DATASETS["respviruses"])

    # --- national weekly ILI consultation incidence -------------------------
    ili = {}
    for r in pick(sent, temporal_type="week", georegion="CH", agegroup="all", sex="all"):
        ili[r["temporal"]] = {
            "incidence": num(r["incValue"]),
            "consultations": num(r["value"]),
            "pct_of_consultations": num(r["prctConsultations"]),
            "trend": r["trend"] if r["trend"] != "NA" else None,
            "complete": r["dataComplete"] == "TRUE",
        }

    # --- weekly laboratory-confirmed cases ---------------------------------
    # Reported at CHFL level (Switzerland + Liechtenstein); that is also the only
    # level at which the influenza A / B split is published, so the stacked chart
    # and the totals stay consistent. Liechtenstein adds a handful of cases a week.
    cases = {}
    for r in pick(oblig, temporal_type="iso_week", georegion=CASES_REGION, agegroup="all",
                  sex="all", type="all", valueCategory="cases"):
        cases[r["temporal"]] = {
            "cases": num(r["value"]),
            "incidence": num(r["incValue"]),
            "trend": r["trend"] if r["trend"] != "NA" else None,
        }

    # Influenza A / B / not-typed split. A + B + unknown sums to the total.
    by_type = defaultdict(dict)
    for t in ("A", "B", "unknown"):
        for r in pick(oblig, temporal_type="iso_week", georegion=CASES_REGION, agegroup="all",
                      sex="all", type=t, valueCategory="cases"):
            by_type[r["temporal"]][t] = num(r["value"])

    # --- PCR positivity among Sentinella swabs ------------------------------
    positivity = {}
    for r in pick(resp, temporal_type="week", georegion="CH", pathogen="influenza",
                  type="all", valueCategory="detections"):
        positivity[r["temporal"]] = {
            "pct_positive": num(r["prctPathogen"]),
            "detections": num(r["value"]),
            "ci_low": num(r["lowerCiPathogen"]),
            "ci_high": num(r["upperCiPathogen"]),
        }
    samples = {}
    for r in pick(resp, temporal_type="week", georegion="CH", pathogen="all",
                  type="all", testResult="all", valueCategory="samples"):
        samples[r["temporal"]] = num(r["value"])

    weeks = sorted(ili)
    latest = weeks[-1]

    # --- 10-season min / median / max envelope for the ILI curve ------------
    # Indexed by ISO week number so it can be drawn behind the current season.
    cutoff_season = season_of(latest)
    prior = sorted({season_of(w) for w in weeks} - {cutoff_season})[-REFERENCE_SEASONS:]
    by_weeknum = defaultdict(list)
    for week in weeks:
        # the running season must not shape the band it is compared against
        if season_of(week) not in prior:
            continue
        value = ili[week]["incidence"]
        if value is not None:
            by_weeknum[int(week.split("-W")[1])].append(value)
    envelope = {
        str(w): {
            "min": round(min(v), 1),
            "median": round(statistics.median(v), 1),
            "max": round(max(v), 1),
            "n": len(v),
        }
        for w, v in sorted(by_weeknum.items())
    }

    # --- per-region current status -----------------------------------------
    regions = []
    for code, (name, cantons) in SENTINELLA_REGIONS.items():
        row = next(pick(sent, temporal=latest, georegion=code, agegroup="all", sex="all"), None)
        value = num(row["incValue"]) if row else None
        regions.append({
            "code": code, "name": name, "cantons": cantons,
            "incidence": value, "stage": stage_for(value),
        })
    regions.sort(key=lambda r: (r["incidence"] is None, -(r["incidence"] or 0)))

    # --- per-canton confirmed cases this week ------------------------------
    canton_rows = []
    for r in pick(oblig, temporal=latest, georegion_type="canton", agegroup="all",
                  sex="all", type="all", valueCategory="cases"):
        canton_rows.append({
            "code": r["georegion"],
            "name": CANTONS.get(r["georegion"], r["georegion"]),
            "cases": num(r["value"]),
            "incidence": num(r["incValue"]),
        })
    canton_rows.sort(key=lambda c: (-(c["cases"] or 0), c["name"]))

    # --- weekly history (all seasons, the page slices what it needs) --------
    history = []
    for week in weeks:
        entry = ili[week]
        history.append({
            "week": week,
            "date": week_start(week),
            "season": season_of(week),
            "weeknum": int(week.split("-W")[1]),
            "ili_incidence": entry["incidence"],
            "cases": (cases.get(week) or {}).get("cases"),
            "cases_a": by_type.get(week, {}).get("A"),
            "cases_b": by_type.get(week, {}).get("B"),
            "cases_untyped": by_type.get(week, {}).get("unknown"),
            "pct_positive": (positivity.get(week) or {}).get("pct_positive"),
            "samples": samples.get(week),
        })

    current_ili = ili[latest]
    current_cases = cases.get(latest, {})
    current_pos = positivity.get(latest, {})
    stage = stage_for(current_ili["incidence"])

    # Season-to-date totals.
    this_season = [h for h in history if h["season"] == cutoff_season]
    season_cases = sum(h["cases"] or 0 for h in this_season)
    season_peak = max((h["ili_incidence"] or 0) for h in this_season)

    current = {
        "week": latest,
        "week_start": week_start(latest),
        "season": cutoff_season,
        "ili_incidence": current_ili["incidence"],
        "ili_consultations": current_ili["consultations"],
        "pct_of_all_consultations": current_ili["pct_of_consultations"],
        "trend": current_ili["trend"],
        "stage": stage["label"],
        "stage_key": stage["key"],
        "stage_level": stage["level"],
        "stage_description": stage["description"],
        "epidemic": (current_ili["incidence"] or 0) >= EPIDEMIC_THRESHOLD,
        "pct_of_epidemic_threshold": (
            round(current_ili["incidence"] / EPIDEMIC_THRESHOLD * 100, 1)
            if current_ili["incidence"] is not None else None
        ),
        "confirmed_cases": current_cases.get("cases"),
        "confirmed_cases_incidence": current_cases.get("incidence"),
        "confirmed_cases_trend": current_cases.get("trend"),
        "pct_positive": current_pos.get("pct_positive"),
        "detections": current_pos.get("detections"),
        "samples_tested": samples.get(latest),
        "season_confirmed_cases": season_cases,
        "season_peak_incidence": season_peak,
        "data_complete": current_ili["complete"],
    }

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": {
            "name": "Federal Office of Public Health (FOPH/BAG) — Infectious Diseases Dashboard",
            "dashboard": "https://www.idd.bag.admin.ch/diseases/influenza/overview",
            "datasets": {k: API.format(dataset=v) for k, v in DATASETS.items()},
            "licence": "opendata.swiss — free use, source must be acknowledged",
            "update_schedule": "weekly, Wednesdays",
        },
        "thresholds": {
            "epidemic": EPIDEMIC_THRESHOLD,
            "stages": [
                {"key": k, "label": lbl, "from": lo, "description": d}
                for lo, k, lbl, d in STAGES
            ],
        },
        "current": current,
        "history": history,
        "envelope": envelope,
        "reference_seasons": prior,
        "regions": regions,
        "cantons": canton_rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(Path(__file__).parent / "site"))
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="print only the current status as JSON (for Home Assistant)")
    args = ap.parse_args()

    data = build()

    if args.print_only:
        print(json.dumps(data["current"]))
        return 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    (out / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
    (out / "current.json").write_text(json.dumps(data["current"], ensure_ascii=False, indent=1))

    template = (Path(__file__).parent / "template.html").read_text()
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    (out / "index.html").write_text(template.replace('"__DATA__"', payload))

    c = data["current"]
    print(f"{c['week']}  ILI {c['ili_incidence']}/100k  [{c['stage']}]  "
          f"{c['confirmed_cases']} confirmed cases  -> {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
