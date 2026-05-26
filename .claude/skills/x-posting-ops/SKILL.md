---
name: x-posting-ops
description: Diagnose and operate the X (Twitter) posting pipeline. Use this skill whenever the user says "投稿されてない", "X のエラー", "今日は何で投稿してない?", "post_predict 動いてる?", "cron が動いてない", or any issue with the EQUINOX Lab X posting cron. The skill investigates GitHub Actions runs for the day, identifies whether cron fired (and when), whether time guards triggered, whether X API returned 403/429, and recommends remedies. Critical knowledge: GitHub Actions cron can be delayed 1-4.5h, X Free tier has 17 posts/24h, time guards intentionally drop late firings.
---

# X Posting Operations

The X posting pipeline has several layers that can each fail independently:

1. **GitHub Actions cron firing** (can be 1-4h delayed, especially on Saturdays)
2. **post_x.py time guards** (intentionally drop fires outside the slot's window)
3. **X API quotas and auth** (Free tier 17 posts/24h, monthly limits, token rotation)
4. **Content fact-check** (verify_article and tweet_fact_check block invalid output)
5. **Duplicate detection** (post_history.json + X API recent tweets check)

This skill traces the flow top-to-bottom to find the failure point.

## When to invoke

User phrases:
- "投稿されてない / 投稿どうなってる"
- "X のエラー" / "403 出てる"
- "cron 動いてない"
- "morning 投稿どこ" / "evening 投稿は?"
- "X の認証問題"

## Workflow

### 1. Establish baseline

What's the user actually expecting to see, and where? Confirm:
- Slot: morning (7:30) / weekday (12:30) / evening (20:00) / post_predict (10:15 weekend)
- Date: today or specific
- Channel: X timeline (twitter.com/<account>) or Threads

### 2. Check the workflow runs

```bash
gh run list --workflow=auto_post_x.yml --limit=10 \
  --json databaseId,createdAt,name,event,conclusion
```

Map each run to slot:
- `30 22 * * 0-4` → 平日朝 (07:30 JST)
- `0 22 * * 5,6` → 土日朝 7:00 (predict)
- `30 3 * * 1-5` → 平日昼 (12:30 JST)
- `0 11 * * 1-5` → 平日夜 (20:00 JST)
- `30 0 * * 6,0` → 土日 09:30 (odds_flash)
- `15 1 * * 6,0` → 土日 10:15 (post_predict)
- `30 6 * * 6,0` → 土日 15:30 (hit_flash)
- `30 8 * * 6,0` → 土日 17:30 (results)
- `30 9 * * 6,0` → 土日 18:30 (refresh_dashboard)

Cron can be delayed 1-4.5h, so cross-reference the *actual* firing time vs the *expected* firing time.

### 3. Read the run log to find the failure

For the relevant run:

```bash
gh run view <run_id> --log | grep -E "X投稿|403|時間ガード|スキップ|期待モード|cmd_"
```

Common failure patterns:

**(a) Time guard triggered** (intentional):
```
⚠️ cmd_evening 時間ガード: 17:04 JST は対象外 (19:00-23:59 のみ)
```
→ This is correct behavior. Cron fired late, guard dropped it to prevent off-slot posting. Tell the user this is normal.

**(b) X 403 Forbidden**:
```
❌ X投稿失敗 [Forbidden]: 403 Forbidden
```
→ Run `scripts/x_diagnose.py` (also exists in repo at scripts/x_diagnose.py) by kicking the `mode=diagnose` dispatch:
```bash
gh workflow run auto_post_x.yml -f mode=diagnose
```
Then check Twitter Dev Portal: https://developer.x.com/en/portal/dashboard for tier and usage.

**(c) Cron didn't fire at all**:
- Today is non-business day, cron schedule excludes it
- GitHub had a global incident (check https://www.githubstatus.com)
- Workflow disabled accidentally

**(d) Content fact_check blocked**:
```
🚫 ファクトチェック不合格のため投稿を中止します
```
→ Read the regex/DB checks output. Usually a stale prediction or empty result.

### 4. Remediate

Match remedy to failure:

| Failure | Remedy |
|---|---|
| Time guard intentional drop | Manually dispatch with SKIP_TIME_GUARD=1 if posting after window is OK |
| X 403 一過性 | Wait for next cron; if persistent, regenerate tokens in Dev Portal |
| X 403 quota hit | Wait until 24h window resets, or upgrade tier |
| Cron didn't fire | Manually dispatch `gh workflow run auto_post_x.yml -f mode=<slot>` |
| Fact_check block | Identify what's wrong with the predictions / DB and fix that |

### 5. Confirm posting

After remediation, verify the post actually went up:
- Read latest tweet via `mode=diagnose` (which calls `client.get_users_tweets`)
- Or check the user's X timeline directly

### 6. Document in CLAUDE.md if it's a new pattern

If the failure was novel (not in past mistake list), add an entry to `## 🐛 過去のミス事例` so it's known going forward.

## Critical constants to remember

- **Time guards** (window when each command is allowed to post):
  - cmd_morning: 7:00-11:59 JST
  - cmd_weekday: 11:00-15:59 JST
  - cmd_evening: 19:00-23:59 JST
  - cmd_predict (post_predict): before 11:00 JST (avoid posting predictions during/after races)
  - cmd_odds_flash: 9:00-10:30 JST

- **X Free tier limits**:
  - 1,500 posts / month
  - 17 posts / 24h rolling window
  - 100 reads / month (the dedup check counts against this)

- **Cron delay tolerance**: ±60 min normal, occasionally 4h+. Time guards are sized to cover 1-2h delay.

## Anti-patterns

- "なんで投稿されてない?" だけ見て、原因を勝手に想像する → 必ず workflow log を読む
- 「X 認証エラー」と決めつけて token regenerate → まず diagnose で実状確認
- Time guard をオフにする → 誤投稿事故 (#15) のリスク復活、最終手段のみ

## Files in this skill

参照する既存スクリプト (repo root):
- `scripts/x_diagnose.py` — X API 認証・rate limit・read 健全性チェック
- `.github/workflows/auto_post_x.yml` — 全 slot の cron schedule
