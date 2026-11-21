---
name: system-integrity-audit
description: Run a full system integrity audit across all modules whenever a major logic change has been deployed. Use this skill after the user lands a substantial change (new confidence formula, new betting strategy, new investiture flow, new schema field) and asks "影響範囲チェック", "整合性監査して", "残ってる古いロジックない?", or any equivalent. The skill greps all caller sites of the changed function, checks for hand-coded threshold残骸 in app.py / post_x.py / generate_note.py, verifies docs (CLAUDE.md, README) reflect the new behavior, validates GitHub Actions workflows still chain correctly, and finally hands back a prioritized punch list. Critical for catching the kind of issue documented as #21 in CLAUDE.md (5 致命的 + 7 警告).
---

# System Integrity Audit

After a big change lands, the model that wrote the change is the worst auditor of it — because they remember the intent, not the implementation reality. This skill exists to be that fresh-eyes pass.

The recurring pattern this prevents: ship a new confidence formula → 2 days later realize `generate_note.py` still has the old hard-coded thresholds → invalid 信頼度 labels in the note article. The audit must happen **before** the rollout settles.

## When to invoke

Trigger after the user (or you) has just landed a non-trivial change. Specifically:
- New scoring/confidence/ROI formula
- New table column or schema field
- New cron schedule / new workflow / new slot
- New cli command in post_x.py
- Major refactor of any shared function (e.g., `evaluate_from_horses`)

The user phrases that should trigger: "影響範囲確認", "整合性監査", "残ってる古いロジックない?", "全部直ったか", "deploy 後の確認".

## Workflow

### 1. Gather change context

Ask the user (or infer from `git log`):
- What changed? (function name, file, the new behavior)
- When did it ship? (commit hash if known)
- What was it replacing? (if any)

If the change is a function signature change, write down the **old signature** and **new signature** — that's what the grep needs.

### 2. Run the audit

Use `scripts/audit_all.py <changed_function_or_concept>` (bundled). It runs in parallel:

**(a) Caller-site grep**: Finds every caller of the changed function across the repo, and shows which still use the old signature or hard-coded values.

**(b) Stale threshold scan**: greps `app.py`, `post_x.py`, `generate_note.py`, `predict.py` for hand-coded thresholds (`if X >= 30` / `if X >= 50` etc.) — these often slip past refactors.

**(c) Doc consistency check**: greps CLAUDE.md and README for terms that should have been updated (old NORMS reference, old workflow names, old cron schedules).

**(d) Workflow chain check**: parses all `.github/workflows/*.yml` to verify:
- Each workflow that restores `actions/cache@v4` also has `git checkout HEAD -- keiba.db` after
- Cron schedules don't double-fire same slot
- preflight_check is called before predict steps
- run_number vs run_id cache key consistency

**(e) Test coverage**: greps `tests/` and `scripts/backtest_*` to identify what would catch a regression.

### 3. Categorize findings

For each finding, tag:
- **❌ 致命的**: produces wrong outputs in production (e.g., generate_note still uses old thresholds → article labels wrong)
- **⚠️ 警告**: redundant code, stale docs, minor inconsistency
- **ℹ️ 情報**: nice-to-have improvements

### 4. Punch list output

Format as markdown table the user can act on:

```markdown
| # | Severity | Location | Issue | Recommended fix |
|---|---|---|---|---|
| 1 | ❌ | generate_note.py:309 | Hard-coded 信頼度 thresholds 50/35/22/12 still present | Replace with evaluate_from_horses() call |
| 2 | ❌ | app.py:870 | Same problem | Same fix |
| 3 | ⚠️ | CLAUDE.md:99 | "NORMS は ML 分布に依存" 古い記述 | "v4 ROI 期待値ベース" に更新 |
```

### 5. Self-fix loop (if user agrees)

Ask: "致命的 X 件、警告 Y 件 検出。全部直しますか? それとも致命的だけ?"

If user agrees, do the fixes in priority order (致命的 → 警告). After each fix, re-run the audit to confirm. Stop when the list is clean.

### 6. Document in CLAUDE.md

Add a new past-mistake entry (#NN) summarizing:
- What was the change
- What the audit found (count + categories)
- What was fixed
- "教訓" — the rule that would have prevented it

This is how we make the system genuinely learn over time.

## Anti-patterns

- Trusting the change author's mental model — they remember intent, not reality
- Spot-checking 1-2 files — must be systematic across all callers
- Skipping the doc-consistency pass — outdated docs cause new mistakes downstream
- "It works in my local test" — production has 9 workflows + 13 slot builders + 3 tables

## Files in this skill

- `scripts/audit_all.py` — Five-pass audit (callers / thresholds / docs / workflows / tests)
