#!/usr/bin/env python3
"""Pull Swiss respiratory-virus surveillance data from the FOPH (BAG) Infectious
Diseases Dashboard open-data API and render a static site + JSON feed.

Influenza is the headline, but the Sentinella PCR panel tests every swab for RSV
and SARS-CoV-2 as well and reports all three in one file, so they are extracted
too — they cost no extra request and keep the page meaningful in summer, when
flu is at background level and the swab count is in single digits.

The same three viruses are also measured daily in wastewater at eleven treatment
plants, which is the one signal here that does not depend on anyone visiting a
doctor. That export has no national figure, so this script builds one; see
WW_MIN_COVERAGE for how, and for what it refuses to publish.

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
    # Sentinella swabs tested by PCR -> share of samples positive, per pathogen.
    "respviruses": "RESPVIRUSES_sentinella",
    # Viral load measured at 11 wastewater treatment plants -> a daily signal that
    # does not depend on anyone seeing a doctor. ~12 MB; by far the slowest fetch.
    "wastewater": "RESPVIRUSES_wastewater",
}

# Wastewater reports one row per plant per day per pathogen, under its own
# category names. Influenza A and B are measured separately and summed here.
WASTEWATER_CATS = {
    "influenza_a": "flu_a",
    "influenza_b": "flu_b",
    "respiratory_syncytial_virus": "rsv",
    "sars-cov-2": "covid",
}

# There is no national wastewater figure in the export — only individual plants —
# so this script builds one. Two things make that harder than a mean:
#
#   1. Plants sample on different days, so between one and eleven report on any
#      given date. Averaging raw daily values tracks *which* plants reported far
#      more than it tracks the virus (58% mean day-over-day swing, against 9%
#      using the per-plant 7-day means the FOPH publishes alongside).
#   2. Plants serve populations from 39k to 471k, so they cannot be weighted
#      equally.
#
# Hence: sum each day's per-plant 7-day means, divide by the population those
# plants actually serve. Days where the reporting plants cover less than this
# share of the monitored population are dropped rather than published thin — at
# the tail of the series a single plant is left, and it swings the national
# figure by a factor of three.
WW_MIN_COVERAGE = 0.5

# The three pathogens on the Sentinella PCR panel, mapped to the short prefix used
# in the output. All three come out of DATASETS["respviruses"].
PATHOGENS = {
    "influenza": ("flu", "Influenza"),
    "respiratory_syncytial_virus": ("rsv", "RSV"),
    "sars-cov-2": ("covid", "SARS-CoV-2"),
}

# Influenza type and subtype rows in that same panel, keyed by (type, subtype).
# B/Yamagata has not been detected anywhere in the world since 2020 but is still
# reported, so it stays in the table as a run of zeros rather than as a gap.
FLU_SUBTYPES = {
    ("A", "all"): ("flu_a", "Influenza A"),
    ("A", "h1n1"): ("flu_a_h1n1", "A(H1N1)pdm09"),
    ("A", "h3n2"): ("flu_a_h3n2", "A(H3N2)"),
    ("A", "unknown"): ("flu_a_unsubtyped", "A, not subtyped"),
    ("B", "all"): ("flu_b", "Influenza B"),
    ("B", "victoria"): ("flu_b_victoria", "B/Victoria"),
    ("B", "yamagata"): ("flu_b_yamagata", "B/Yamagata"),
    ("B", "unknown"): ("flu_b_unlineaged", "B, lineage not determined"),
}

# The subtypes worth naming as "dominant"; the aggregate and residual rows above
# are not candidates.
DOMINANT_CANDIDATES = ["flu_a_h1n1", "flu_a_h3n2", "flu_b_victoria", "flu_b_yamagata"]

# Both surveillance systems break down by age on the same bands. Sentinella
# publishes age only at national level, not per region.
AGE_GROUPS = ["0 - 4", "5 - 14", "15 - 29", "30 - 64", "65+"]
AGE_SLUGS = {"0 - 4": "0_4", "5 - 14": "5_14", "15 - 29": "15_29",
             "30 - 64": "30_64", "65+": "65p"}

SEXES = {"female": "Female", "male": "Male"}

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
    req = urllib.request.Request(url, headers={"User-Agent": "swiss-virus-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        text = resp.read().decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def num(value: str | None, digits: int = 2):
    """CSV uses the string 'NA' for missing values.

    Rounded on the way in: the API returns percentages at full float precision
    (28.169014084507 for what is 20 detections out of 71), and carrying those
    digits into the JSON roughly triples the size of the page for no gain.
    """
    if value is None or value in ("NA", "", "NULL"):
        return None
    try:
        return round(float(value), digits)
    except ValueError:
        return None


def week_start(week: str) -> str:
    year, w = week.split("-W")
    return dt.date.fromisocalendar(int(year), int(w), 1).isoformat()


def iso_week(date: str) -> str:
    """'2025-01-15' -> '2025-W03'. Wastewater is dated, not weeked."""
    year, week, _ = dt.date.fromisoformat(date).isocalendar()
    return f"{year}-W{week:02d}"


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


def columnar(rows: list[dict], keys: list[str]) -> dict:
    """Array-of-objects -> object-of-arrays, aligned by index.

    Repeating thirty key names across three hundred weekly rows costs several
    times what the numbers themselves do. The page inflates this back into rows
    on load; `weeks` is the shared index.
    """
    return {k: [r.get(k) for r in rows] for k in keys}


def ww_trend(series: list, idx: int, span: int = 7, cutoff: float = 0.2) -> str | None:
    """Rising / falling / stagnant for a wastewater series, against `span` days ago.

    The per-plant `trend` column in the export describes one plant, so a national
    trend has to be derived. Wastewater is noisy even after smoothing; anything
    inside +/-20% over a week is called stagnant.
    """
    if idx < span:
        return None
    now, then = series[idx], series[idx - span]
    if now is None or then is None or not then:
        return None
    change = (now - then) / then
    return "rising" if change > cutoff else "falling" if change < -cutoff else "stagnant"


def pick(rows, **filters):
    for row in rows:
        if all(row.get(k) == v for k, v in filters.items()):
            yield row


def wastewater_panel(rows: list[dict]) -> tuple[dict, list[dict]]:
    """National daily viral load per pathogen, plus the plants behind it.

    Returns a columnar series in *thousands of gene copies per person per day*
    (`value` in the export is copies/day for the whole catchment), and the site
    list. See WW_MIN_COVERAGE for why days get dropped.
    """
    sites = {}
    for r in rows:
        sites.setdefault(r["georegion2"], {
            "plant": r["georegion2"], "canton": r["georegion1"],
            "population": int(num(r["pop"], 0) or 0)})
    monitored = sum(s["population"] for s in sites.values())

    loads = defaultdict(float)          # (date, key) -> summed 7-day mean
    covered = defaultdict(float)        # (date, key) -> population behind it
    reporting = defaultdict(set)        # (date, key) -> plants
    for r in rows:
        key = WASTEWATER_CATS.get(r["valueCategory"])
        mean7d = num(r["valueMean7d"], 0)
        if key is None or mean7d is None:
            continue
        stamp = (r["temporal"], key)
        loads[stamp] += mean7d
        covered[stamp] += num(r["pop"], 0) or 0
        reporting[stamp].add(r["georegion2"])

    def per_capita(date: str, key: str):
        pop = covered[(date, key)]
        if not pop or pop / monitored < WW_MIN_COVERAGE:
            return None
        return round(loads[(date, key)] / pop / 1000, 1)

    # Influenza is carried as A and B; the page wants them together, so that the
    # wastewater and swab charts show the same three pathogens on the same hues.
    dates = sorted({d for d, _ in loads})
    series = {"dates": [], "seasons": [], "plants": [], "coverage": [],
              "flu": [], "flu_a": [], "flu_b": [], "rsv": [], "covid": []}
    for date in dates:
        flu_a, flu_b = per_capita(date, "flu_a"), per_capita(date, "flu_b")
        row = {"flu_a": flu_a, "flu_b": flu_b,
               "flu": None if flu_a is None or flu_b is None else round(flu_a + flu_b, 1),
               "rsv": per_capita(date, "rsv"), "covid": per_capita(date, "covid")}
        if all(v is None for v in row.values()):
            continue                    # nothing cleared the coverage bar that day
        series["dates"].append(date)
        series["seasons"].append(season_of(iso_week(date)))
        series["plants"].append(len(reporting[(date, "flu_a")]) or None)
        series["coverage"].append(round(covered[(date, "flu_a")] / monitored * 100))
        for key, value in row.items():
            series[key].append(value)

    return series, sorted(sites.values(), key=lambda s: -s["population"])


def build() -> dict:
    sent = fetch(DATASETS["sentinella"])
    oblig = fetch(DATASETS["oblig"])
    resp = fetch(DATASETS["respviruses"])
    wastewater, ww_sites = wastewater_panel(fetch(DATASETS["wastewater"]))

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

    # --- PCR results from the Sentinella swab panel --------------------------
    # One swab is tested for all three pathogens, so the per-pathogen positivity
    # rates share a denominator and can exceed 100% summed (co-infections).
    panel = defaultdict(dict)
    for api_name, (short, _label) in PATHOGENS.items():
        for r in pick(resp, temporal_type="week", georegion="CH", pathogen=api_name,
                      type="all", subtype="all", valueCategory="detections"):
            panel[r["temporal"]].update({
                f"{short}_pct": num(r["prctPathogen"]),
                f"{short}_n": num(r["value"]),
                f"{short}_ci_low": num(r["lowerCiPathogen"]),
                f"{short}_ci_high": num(r["upperCiPathogen"]),
                # The FOPH's own 3-week rolling mean; steadier than the weekly
                # rate when the swab count drops off in the off-season.
                f"{short}_mean3w": num(r["prctPathogenMean3w"]),
            })

    # Influenza A / B and their subtypes, with each one's share of all influenza
    # detections that week (prctPathogenType for the types, prctPathogenSubtype
    # for the subtypes — both are shares of influenza, not of samples).
    for (typ, sub), (key, _label) in FLU_SUBTYPES.items():
        share_col = "prctPathogenType" if sub == "all" else "prctPathogenSubtype"
        for r in pick(resp, temporal_type="week", georegion="CH", pathogen="influenza",
                      type=typ, subtype=sub, valueCategory="detections"):
            panel[r["temporal"]][key] = num(r["value"])
            panel[r["temporal"]][f"{key}_share"] = num(r[share_col])

    # Denominators: swabs tested, and swabs positive for at least one pathogen.
    for r in pick(resp, temporal_type="week", georegion="CH", pathogen="all",
                  type="all", testResult="all", valueCategory="samples"):
        panel[r["temporal"]]["samples"] = num(r["value"])
    for r in pick(resp, temporal_type="week", georegion="CH", pathogen="any",
                  type="all", testResult="positive", valueCategory="samples"):
        panel[r["temporal"]]["positive_any"] = num(r["value"])
        panel[r["temporal"]]["pct_positive_any"] = num(r["prctSamples"])

    samples = {w: p.get("samples") for w, p in panel.items()}

    # --- FOPH's own 10-year band for confirmed cases -------------------------
    # Unlike the ILI envelope below (which this script computes), the case
    # min/median/max is published. It is a rolling window: only the most recent
    # ~250 weeks carry it, so older history keeps None here.
    cases_band = defaultdict(dict)
    for stat, key in (("min_value_10y", "min"), ("median_value_10y", "median"),
                      ("max_value_10y", "max")):
        for r in pick(oblig, temporal_type="iso_week", georegion=CASES_REGION,
                      agegroup="all", sex="all", type="all", valueCategory=stat):
            cases_band[r["temporal"]][key] = num(r["value"])

    # --- age and sex breakdowns ---------------------------------------------
    # Sentinella publishes these at national level only; the mandatory-reporting
    # case counts carry the same bands at CHFL level.
    ili_by_age = defaultdict(dict)
    for group in AGE_GROUPS:
        for r in pick(sent, temporal_type="week", georegion="CH", agegroup=group, sex="all"):
            ili_by_age[r["temporal"]][group] = num(r["incValue"])
    cases_by_age = defaultdict(dict)
    for group in AGE_GROUPS:
        for r in pick(oblig, temporal_type="iso_week", georegion=CASES_REGION,
                      agegroup=group, sex="all", type="all", valueCategory="cases"):
            cases_by_age[r["temporal"]][group] = {
                "cases": num(r["value"]), "incidence": num(r["incValue"])}
    ili_by_sex = defaultdict(dict)
    for sex in SEXES:
        for r in pick(sent, temporal_type="week", georegion="CH", agegroup="all", sex=sex):
            ili_by_sex[r["temporal"]][sex] = num(r["incValue"])

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
            "pct_positive": panel.get(week, {}).get("flu_pct"),
            "samples": samples.get(week),
        })

    # --- the PCR panel as its own series -------------------------------------
    # Kept separate from `history` rather than merged into it: the panel only
    # starts in 2020-W40, while the ILI history reaches back to 2013, so merging
    # would pad two thirds of the rows with nulls for three pathogens' worth of
    # fields. The page slices this by season the same way it slices `history`.
    panel_weeks = sorted(panel)
    panel_rows = [panel[w] for w in panel_weeks]
    panel_fields = ["samples", "positive_any", "pct_positive_any"]
    for short, _label in PATHOGENS.values():
        panel_fields += [f"{short}_pct", f"{short}_n", f"{short}_ci_low",
                         f"{short}_ci_high", f"{short}_mean3w"]
    for key, _label in FLU_SUBTYPES.values():
        panel_fields += [key, f"{key}_share"]
    respiratory = {
        "weeks": panel_weeks,
        "dates": [week_start(w) for w in panel_weeks],
        "seasons": [season_of(w) for w in panel_weeks],
        "weeknums": [int(w.split("-W")[1]) for w in panel_weeks],
        **columnar(panel_rows, panel_fields),
    }

    # --- age breakdown for the current week ----------------------------------
    age_total = sum(v or 0 for v in ili_by_age.get(latest, {}).values())
    age_groups = []
    for group in AGE_GROUPS:
        incidence = ili_by_age.get(latest, {}).get(group)
        case_row = cases_by_age.get(latest, {}).get(group, {})
        age_groups.append({
            "group": group,
            "ili_incidence": incidence,
            # Share of the summed per-age incidence, i.e. how the burden is
            # distributed across bands — not a share of the national rate.
            "ili_share": round(incidence / age_total * 100, 1)
            if incidence is not None and age_total else None,
            "cases": case_row.get("cases"),
            "cases_incidence": case_row.get("incidence"),
        })

    age_history = {
        "weeks": weeks,
        **{f"ili_{AGE_SLUGS[g]}": [ili_by_age.get(w, {}).get(g) for w in weeks]
           for g in AGE_GROUPS},
        **{f"cases_{AGE_SLUGS[g]}": [cases_by_age.get(w, {}).get(g, {}).get("cases")
                                     for w in weeks]
           for g in AGE_GROUPS},
    }

    current_ili = ili[latest]
    current_cases = cases.get(latest, {})
    current_pos = panel.get(latest, {})
    stage = stage_for(current_ili["incidence"])

    # Which influenza subtype is driving this week, if any were detected at all.
    labels = {key: label for key, label in FLU_SUBTYPES.values()}
    top_n, top_key = max((current_pos.get(k) or 0, k) for k in DOMINANT_CANDIDATES)
    dominant = labels[top_key] if top_n else None

    # Latest wastewater day. It runs on its own clock — daily, and a few days
    # behind today because the 7-day mean needs a full window — so it carries its
    # own date rather than being folded into the reporting week.
    ww_last = len(wastewater["dates"]) - 1
    ww_current = {"wastewater_date": wastewater["dates"][ww_last] if ww_last >= 0 else None,
                  "wastewater_plants": wastewater["plants"][ww_last] if ww_last >= 0 else None}
    for key in ("flu", "rsv", "covid"):
        ww_current[f"wastewater_{key}"] = wastewater[key][ww_last] if ww_last >= 0 else None
        ww_current[f"wastewater_{key}_trend"] = ww_trend(wastewater[key], ww_last)

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
        "pct_positive": current_pos.get("flu_pct"),
        "detections": current_pos.get("flu_n"),
        "samples_tested": samples.get(latest),
        # Same swabs, other two pathogens on the panel. Below the flu fields so
        # that existing Home Assistant templates keep working unchanged.
        "rsv_pct_positive": current_pos.get("rsv_pct"),
        "rsv_detections": current_pos.get("rsv_n"),
        "covid_pct_positive": current_pos.get("covid_pct"),
        "covid_detections": current_pos.get("covid_n"),
        "pct_positive_any": current_pos.get("pct_positive_any"),
        "positive_any": current_pos.get("positive_any"),
        "flu_a_detections": current_pos.get("flu_a"),
        "flu_b_detections": current_pos.get("flu_b"),
        "dominant_subtype": dominant,
        "cases_median_10y": cases_band.get(latest, {}).get("median"),
        **ww_current,
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
        "pathogens": [
            {"key": short, "label": label, "dataset": DATASETS["respviruses"]}
            for short, label in PATHOGENS.values()
        ],
        "flu_subtypes": [{"key": key, "label": label} for key, label in FLU_SUBTYPES.values()],
        "current": current,
        "history": history,
        "respiratory": respiratory,
        "envelope": envelope,
        # The FOPH's published 10-year band for confirmed cases, keyed by week.
        # Sparse on purpose: it is a rolling window covering only recent weeks.
        "cases_band": {w: b for w, b in sorted(cases_band.items()) if b},
        "reference_seasons": prior,
        "regions": regions,
        "cantons": canton_rows,
        # Daily viral load, thousands of gene copies per person per day. Columnar,
        # like `respiratory`; `dates` is the shared index.
        "wastewater": wastewater,
        "wastewater_sites": ww_sites,
        "wastewater_population": sum(s["population"] for s in ww_sites),
        "age_groups": age_groups,
        "age_history": age_history,
        "sex_current": [
            {"sex": key, "label": label, "ili_incidence": ili_by_sex.get(latest, {}).get(key)}
            for key, label in SEXES.items()
        ],
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
