---
name: race-data-backfill
description: Detect missing race data in the EQUINOX Lab DB and dispatch seed_historical workflows to fill gaps. Use this skill whenever the user says "データが足りない", "○○年のデータ取って", "過去データ揃ってる?", "backfill して", or whenever a data-driven analysis hits insufficient sample size. The skill audits year-by-year coverage in `races` and `predictions_cache`, dispatches `gh workflow run seed_historical.yml -f year=YYYY` for each missing year, monitors progress, handles the rebase-conflict bug (`--theirs` is correct for binary keiba.db, not `--ours`), and reports back. Never proceeds with stale gap assumptions — always reverifies from origin/main.
---

# Race Data Backfill

The DB is the foundation of everything. When it has gaps, analyses, articles, and predictions all silently produce wrong results. This skill catches and fixes data gaps proactively.

The recurring failure mode: ship analysis assuming "we have 6 years" → user catches that 2021 is missing → kick backfill → realize the deploy step had a `--ours` bug that ate the new data → re-kick → wait again. This skill exists to do the audit *first* and the fix correctly.

## When to invoke

Trigger when:
- User mentions data shortage ("データ足りない", "2021ないの?", "全部とろうよ")
- Analysis hits N < threshold for important metrics
- Before generating an article about a race (the race-article-writer skill calls this)
- Before running a backtest that claims to span >5 years

## Workflow

### 1. Audit coverage

Run `scripts/audit_db_coverage.py` (bundled). It outputs JSON:

```json
{
  "year_coverage": {2020: 3456, 2021: 278, 2022: 3456, ...},
  "predictions_cache_coverage": {...},
  "missing_years": [2021],
  "race_specific_gaps": {"日本ダービー": [2021], "目黒記念": [2021]},
  "recommended_actions": ["gh workflow run seed_historical.yml -f year=2021"]
}
```

### 2. Decide kick vs proceed

The user has two reasonable choices:

- **Kick and wait**: 1 year takes 60-90 min. If the user needs accurate "過去6年" analysis, this is the right call.
- **Kick and proceed**: kick the backfill, then write/analyze with available years. The article/report must honestly say "2021はDB欠落のため対象外". `verify_article.py` allows this when keywords like "DB欠落 / 対象外 / 未登録" are present.

Ask the user which one they want. Default to "kick and proceed" if they don't reply quickly — it's the higher-throughput choice.

### 3. Kick the workflow correctly

```bash
gh workflow run seed_historical.yml -f year=2021
```

After kicking, get the run_id:

```bash
sleep 3 && gh run list --workflow=seed_historical.yml --limit=1 --json databaseId --jq '.[0].databaseId'
```

Tell the user the run_id and ETA.

### 4. Monitor (optional, background)

The user can choose to monitor with `gh run watch <run_id> --interval=60`, but typically this runs unattended. The workflow:

1. Scrapes year's races from netkeiba (~3500 races / year)
2. Writes to keiba.db
3. Commits and pushes with rebase+retry (5 attempts)

### 5. Verify success

When done, **verify by reading origin/main keiba.db** (not local DB):

```bash
git fetch origin main && git checkout origin/main -- keiba.db
sqlite3 keiba.db "SELECT substr(race_date,1,4) yr, COUNT(*) FROM races WHERE race_date >= '2020-01-01' GROUP BY yr ORDER BY yr;"
```

If the year is still < 100 races: **the deploy step failed silently**. Check the workflow log for:
- `1 file changed, 0 insertions(+), 0 deletions(-)` — the rebase `--ours` bug ate the data
- `error: could not apply` — merge conflict

In either case, the fix is in `seed_historical.yml` — make sure the deploy step uses `git checkout --theirs keiba.db` (not `--ours`).

### 6. Race-specific check

For specific races, also verify:

```sql
SELECT substr(race_date,1,4) yr, race_id, race_name
FROM races WHERE race_name LIKE '%日本ダービー%' ORDER BY race_date DESC;
```

If the race itself is still missing for the year, the year backfill succeeded but the race wasn't in the scrape (rare; check netkeiba race_list URL pattern).

## Anti-patterns

- "Backfill kicked → success" without verifying origin/main DB → False sense of progress
- Re-running the same year backfill 3 times without fixing the deploy bug → wastes 3 hours
- Trusting `🚀 deployed` in workflow log alone — also check `git log` for the `📦 Seed historical data YYYY` commit

## Files in this skill

- `scripts/audit_db_coverage.py` — Full coverage report
