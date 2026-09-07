# Refreshing the tracker

This thing is meant to run untended. When it stops, it is almost always one of the
four things below. Start at the top — the first one is by far the most likely.

## 1. GitHub disabled the schedule

**Symptom:** the site is stuck on an old week, and the Actions tab shows no runs for
a while. You probably also got an email titled *"[repo] Scheduled workflow disabled"*.

GitHub turns off scheduled workflows after **60 days of repository inactivity**.
Building and deploying does not count as activity — only pushes do — so a repo that
just sits there rebuilding itself will be switched off roughly every two months.

**Fix now:** Actions tab → *Update flu data* → **Enable workflow**, then *Run workflow*
to catch up.

**Fix for good:** make the Wednesday run push a commit, which resets the 60-day clock
every week. This takes two changes, and the first one is easy to miss.

`site/` is in [`.gitignore`](.gitignore), so `git add site/data.json` fails outright
(*"The following paths are ignored…"*, exit code 1) and takes the whole step down with
it. Git cannot re-include a file whose parent directory is excluded, so ignore the
directory's *contents* and name the two exceptions:

```gitignore
site/*
!site/data.json
!site/current.json
```

`site/index.html` stays ignored — it is a build artefact, and at ~460 kB a week it is
not something to keep 52 copies of a year. The two JSON files are the feed itself, so
a history of them is worth having.

Then, in [`.github/workflows/update.yml`](.github/workflows/update.yml), set
`contents: write` in the `permissions:` block and add a step after the build:

```yaml
      - name: Commit data snapshot
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add site/data.json site/current.json
          git diff --quiet --cached || git commit -m "data: $(date -u +%Y-W%V)"
          git push
```

If you would rather keep the repository free of build output entirely, swap the `add`
and `commit` lines for `git commit --allow-empty -m "keepalive: $(date -u +%Y-W%V)"`.
That resets the clock just as well and stores nothing.

## 2. The data stopped moving, but everything is green

**Symptom:** runs succeed, Home Assistant is happy, and the week number has not
changed in a fortnight.

The FOPH publishes on Wednesdays for the *previous* week, so `week_start` in
`current.json` is normally 9–16 days behind today. Beyond about three weeks,
something upstream has changed. Check the
[dashboard](https://www.idd.bag.admin.ch/diseases/influenza/overview) — if the site
itself is current, the export API has moved on without us and `fetch.py` is silently
selecting nothing. Go to point 3.

Nothing in the pipeline notices this on its own. If that bothers you, have
`build()` compare `week_start(latest)` against today and raise when the gap is too
wide — a red run is an email, a stale one is silence.

## 3. A run failed

**Symptom:** red run in the Actions tab. Open it and read the traceback.

`IndexError` on `weeks[-1]` in [`fetch.py`](fetch.py) means one of the CSV filters
matched no rows: a column or a value in the export changed. Fetch a dataset by hand
and look at what actually comes back:

```sh
curl -s "https://idd.bag.admin.ch/api/v1/export/latest/INFLUENZA_sentinella/csv" | head -3
```

Then reconcile the header against the `pick(...)` filters in `build()` — the usual
suspects are `georegion`, `temporal_type`, `agegroup` and `valueCategory`, plus
`pathogen`, `type` and `subtype` on the PCR panel. Note that most of those filters
fail *quietly*: only the ILI series ends in `weeks[-1]` and raises. If a pathogen or
subtype value is renamed upstream, that column simply goes null and the page renders
a flat line at zero. Compare a peak week against the dashboard if a series looks
suspiciously empty. Run
`python3 fetch.py` locally until it prints a sensible line, then push.

Anything network-shaped (timeout, 5xx) needs no action: the workflow runs twice on
Wednesday, four hours apart, and the second attempt usually clears it.

## 4. The site is gone or the feed 404s

**Symptom:** Home Assistant sensors unavailable, Pages URL dead.

Check **Settings → Pages → Source** is still *GitHub Actions*. Turning the repo
private, or renaming it, will also change or kill the URL — if you rename, update the
`resource:` in [`homeassistant/flu_tracker.yaml`](homeassistant/flu_tracker.yaml)
to match.

Note that a failed run leaves the last good deploy in place, so the site never
disappears just because a build broke. If the page is genuinely gone, it is a Pages
setting, not the data.

## Checking it yourself

Everything is stdlib, so a local run is the fastest diagnostic:

```sh
python3 fetch.py            # prints the current week + stage to stderr
python3 fetch.py --print    # the exact JSON Home Assistant reads
```

If that works locally but not in Actions, the problem is the workflow or Pages,
not the data.
