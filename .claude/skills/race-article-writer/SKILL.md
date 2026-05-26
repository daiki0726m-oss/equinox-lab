---
name: race-article-writer
description: Generate a fact-checked, AI-driven note-style article (markdown) for a specific Japanese horse race. Use this skill whenever the user asks for a race article, race preview, race analysis, "○○記念の記事", "ダービーの予想記事", "G1/G2/G3 の記事を書いて", or anything that requires generating an article-length write-up about an upcoming JRA race. The skill autonomously checks DB year coverage, kicks backfill workflows for missing years, queries past winners/bloodlines/prev races, cross-references entries against course stats, picks AI-noted horses by objective rules, drafts the article, and verifies it with scripts/verify_article.py — looping until factual consistency passes. Strict rules: horse numbers only after the post-position draw (Friday 11:00 JST), marks (◎○▲) only on Saturday morning's final prediction, all "N勝/M" claims must match the in-text year-by-year enumeration.
---

# Race Article Writer

This skill generates AI-driven race-preview articles for the EQUINOX Lab horse-racing system. The core goal is **eliminating the rework loop** caused by:

- Data gaps the writer didn't check before writing ("Why only 4 years when we have 6?")
- Numerical claims that contradict the in-text enumeration ("1番人気3勝/4 vs only 2 in the list")
- Premature display of post-draw fields (horse numbers, ◎○▲ marks) before they are determined in reality
- Hand-written articles drifting from what the DB actually says

The skill exists so the model — not the user — catches these.

## When to invoke

Trigger when the user asks for an article about a specific race. Phrases to watch for: race-name + 記事/予想/分析/プレビュー, "○○杯の記事書いて", "ダービーの note 記事", "○○記念の AI 予想", etc.

The race may be:
- An **upcoming** race (entries scraped but post-draw not yet; current week)
- A **past** race (results in DB)

Both branches share the same workflow; the rules section handles the timing differences.

## Workflow

### 1. Capture inputs

The user usually gives you a race name (e.g. "目黒記念"). If unclear, ask for:
- Race name (kanji, exact as in `races.race_name`)
- Target date (helps disambiguate year and check if it's past/upcoming)
- Target output (default: `articles/<slug>_<year>_note.md`)

### 2. Audit DB coverage — and fix gaps

This is the autonomy point. Don't write first and discover gaps later.

Run `scripts/check_coverage.py <race_name>` (bundled with this skill). It reports:
- Year-by-year race counts in `races` (2020-current)
- Which past editions of the named race are in DB
- Recommended actions if gaps exist

If gaps are detected (a year has < 100 total races, or a relevant past edition is missing):
- Kick `gh workflow run seed_historical.yml -f year=<missing>` to trigger backfill
- The workflow takes ~60-90 min per year
- **Do not block the user**. Tell them what's been kicked, and either (a) proceed with the remaining years honestly noted, or (b) wait and resume later, depending on their preference

When proceeding without all years, the article must say something like "過去 N 年 (2020/2022/2023/2024/2025、2021 は DB 未取得のため対象外)" — `verify_article.py`'s year-continuity check allows this with the keywords "DB欠落", "対象外", "未登録", "未収集", "除く", "含まない".

### 3. Pull facts from DB

Use `scripts/pull_race_facts.py <race_name> [--years 6]` (bundled). It returns a JSON blob with:
- **winners**: `[{year, horse_name, popularity, odds, sire, damsire, prev_race, prev_finish, impost, ...}]`
- **course_stats**: `{venue, surface, distance, sire_top, post_position_bias, pace_decisive, jockey_top}` for the race's course (eg. 東京芝2500m)
- **entries**: `[{horse_number, horse_name, sire, damsire, jockey_name, impost, ...}]` for the upcoming edition (if available). When entries lack post-draw `horse_number`, the field will be 0 or marked provisional.
- **gaps**: `[year, ...]` — explicit list of years the named race is missing in DB

Treat this JSON as the **single source of truth**. Every number in the article must trace back to a field here.

### 4. Article structure

Use this proven 3-axis template. It keeps the article focused on what the data actually shows.

```
# <race_name><year> — AIが過去N年のデータから導き出す3つの軸

<lede: race outline, course, dates, why this race is worth analyzing>

## 軸①: <strongest bloodline / structural finding>
<top sire by complot rate + which past winners match + AI interpretation>

## 軸②: <人気帯分布 or 配当傾向>
<winner pop list, derived stat, AI takeaway>
※ The summary stat MUST match the in-text enumeration. If the list shows 2 wins for 1人気, do not write "3勝/4". Aggregate by reading the list.

## 軸③: <prev-race / rotation / impost pattern>
<winner prev races, derived pattern, AI takeaway>

## さらに見えてきた数字 (補助データ)
<post position bias, pace decisive rate, impost band — short bullets>

## AIが注目する3頭
※ <draw status disclaimer — see Rules section>
1️⃣ <horse_name> — <father> (course複勝率 X%) / 斤量 Y / <one-line reason>
2️⃣ <horse_name> — ...
3️⃣ <horse_name> — ...

### <optional: 鞍上スコア最強候補, アナ候補 etc.>

## サンプルの限界と注意点
- 欠落年度を明示 (verify_article がチェックする)
- 出走予定馬の血統未登録があれば明示
- コース統計の母集団 (G2 限定 vs OP 含む) を明示

## 結論
<3 軸を要約 + AI が誰を見ているか + 「土曜朝に確定予想を配信」>
```

### 5. Strict rules (the hard ones)

These are the rules that get violated the most. Internalize them.

**(a) Horse numbers (馬番) are only allowed after Friday 11:00 JST post-position draw.**

- For Saturday races (weekday=5): drawn Friday
- For Sunday races (weekday=6): drawn Friday (two days before)
- Before draw: write "キングズパレス" (name only). Never "7番 キングズパレス".
- After draw: "7番 キングズパレス" is fine.

If you're not 100% sure the draw has happened by the article's `date_phrase`, **omit the number**. The downside of omitting is zero; the downside of writing a wrong number is the user gets misled.

**(b) Marks (◎○▲△×注) are only allowed in the Saturday-morning `post_predict` output.**

- The article being written here is **pre-prediction content**. Use "1️⃣ 2️⃣ 3️⃣" or "注目馬 / 対抗 / 単穴" or "最有力候補".
- Never label horses "◎ ○ ▲" in this article. If the user pastes ◎-format from somewhere else, push back.
- Add a disclaimer near the picks: "確定買い目 (◎○▲) は土曜朝の最終予想で発表"

**(c) Numerical claims must match the in-text enumeration.**

This was the bug that produced "1番人気が3勝/4" when the list showed only 2. The rule:
- When writing "X人気がN勝", count occurrences of "X人気" in the enumeration just above. If they don't match, fix the claim — never the list.
- `verify_article.py`'s `check_numeric_claim_consistency` will catch this. It's not optional.

**(d) Term hygiene.**

- "飛び馬" → "飛んだ1人気" / "凡走馬"
- "バケット" → "区分" / "パターン"
- "上り" → "上がり" (correct kanji)
- "AI予測キャッシュ未生成" → never appear in user-facing text; show alternative content instead
- "様 / ご" overuse → avoid corporate softening, keep voice direct

### 6. Verify and iterate

Run:

```bash
python3 scripts/verify_article.py articles/<filename>.md --race "<race_name>"
```

Possible outcomes:
- ✅ No issues → done, commit
- ⚠️ Only warnings → review them; many are false positives on katakana common nouns (キンカメ, スタミナ, スタート). If they're all common-noun false positives, document this in the commit message and proceed.
- ❌ Critical issues (`🚫` prefix) → MUST fix. Do not commit until clean. Re-run after each fix.

Common critical issues and the fix:
- "年度抜け" → either add the missing year's enumeration, or add a "DB欠落" disclaimer for that year
- "数値不整合" (新しい check) → recount the in-text enumeration, fix the summary stat to match
- "プレースホルダー残存" → remove `[URL]`, `[TODO]` etc.

### 7. Commit

When verification passes:

```bash
git add articles/<filename>.md
git commit -m "docs: <race> <year> AI記事 — <one-line summary>

<details>"
git push origin HEAD:main
```

Include in the commit message:
- What rules were followed (馬番omit, marks omit, etc.)
- Any data gaps and why they were proceeded around
- The verify output (clean / warnings-only)

## Examples

### Example 1: User asks for an article about an upcoming race

```
User: 目黒記念の note 記事を書いて
```

Steps:
1. `scripts/check_coverage.py 目黒記念` → sees 2021 is missing
2. Kick `gh workflow run seed_historical.yml -f year=2021` (background)
3. Tell user: "2021 backfill を kick しました (~60-90分)。先に 5年分 (2020/2022/2023/2024/2025) で記事ドラフト書きます。完了後に 6年版に差し替え可能です。"
4. `scripts/pull_race_facts.py 目黒記念 --years 6` → fact JSON
5. Draft article using the template
6. Run `verify_article.py` → fix any critical issues
7. Commit

### Example 2: Verifier blocks on numerical mismatch

```
verify_article: 🚫 数値不整合: 「1番人気が3勝」と記載があるが、本文中の事実列挙では 2件 のみ検出
```

Fix: rewrite the section heading to match the list. Don't rewrite the list to match the heading — the list is ground truth.

```
Before: ## 軸②: 1番人気は信頼できる (3勝/4)
After:  ## 軸②: 4人気以内で4勝/4 (1人気2勝/2人気1勝/4人気1勝)
```

## Files in this skill

- `scripts/check_coverage.py` — DB year-coverage audit; dispatches workflows if gaps
- `scripts/pull_race_facts.py` — Single source of truth: every fact in the article comes from this
- `references/article_template.md` — Reference template if the workflow needs a refresher

The bundled scripts assume working directory is the repo root and `keiba.db` is at the expected path.
