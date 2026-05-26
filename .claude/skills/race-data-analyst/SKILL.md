---
name: race-data-analyst
description: Run backtests, ROI analysis, signal discovery, and confidence-logic validation on the EQUINOX Lab horse-racing DB. Use this skill whenever the user asks for ROI analysis, backtest results, "信頼度の検証", "ROIの内訳", "傾向の発掘", "バックテストして", "ML予測の精度を見たい", "○○ロジックを変えてみた効果", or any data-driven investigation of past races. The skill autonomously queries `predictions_cache` and `results`, computes ROI by confidence tier (S/A/B/C/D) and bet type (馬連/ワイド/三連複/三連単), discovers patterns by cross-tab analysis, validates statistical significance, and produces a markdown report. Never accepts hand-tuned numbers — every claim cites a SQL query.
---

# Race Data Analyst

This skill is for data-driven analysis of past races. It exists to prevent the recurring trap of "AI が因果を作る" — N=2 events being labeled as "傾向" when they're noise.

The core mindset:
- **No claim without baseline comparison.** "1人気が飛んだ" is meaningless until we know the base rate.
- **No "共通点" with N < 3.** Sample-size minimum is explicit.
- **No ROI claim without a denominator and a date range.**

## When to invoke

Trigger when the user asks about:
- Backtests ("3-5月のROIを出して", "新ロジックで再計算")
- Confidence tier validation ("Sの ROI が本当に高いか", "Bが逆転してないか")
- Pattern discovery ("○○の傾向は?", "特定条件で複勝60%超のパターン")
- Logic comparisons ("旧版 vs 新版の比較", "閾値を変えた効果")

## Workflow

### 1. Frame the question

Before any SQL, write down:
- **Hypothesis**: what would success/failure look like
- **Sample**: which races, which date range, which bet types
- **Baseline**: what is the comparison group ("全体平均" or "旧ロジック")

If the user's question is vague, ask once. Example:
> User: 「信頼度の検証して」
> You: 「対象期間は 3-5月で OK? 比較ベースは『全体平均』『旧 v2 ロジック』のどちら?」

### 2. Run the analysis

Use the bundled scripts under `scripts/`:

- `run_backtest.py` — Compute ROI by confidence tier × bet type, with sample sizes
- `find_significant_patterns.py` — Discover条件 with complot rate > baseline + 15pt
- `compare_versions.py` — Compare old vs new logic outputs head-to-head

All output JSON to stdout for further processing.

### 3. Statistical sanity gates

Before quoting a number in a report, check:

| Claim type | Min sample | Notes |
|---|---|---|
| ROI by tier | n ≥ 20 races per tier | Less than this and tier confidence is noise |
| "○○の複勝率N%" | n ≥ 10 outings | Below this, label as "(参考)" |
| "○○の傾向" | n ≥ 30 races for the condition | Below this, do not call it a 傾向 |
| Pattern × bet type | n ≥ 5 hits | Below this, do not extrapolate ROI |

If sample falls short, **don't write the claim**. Instead, write what you'd need ("もう X 戦サンプルが必要") and recommend backfill.

### 3.5 単日支配チェック (CRITICAL — 2026-05-26 教訓 #25)

**「期間 ROI = 単日の lucky day に支配されていないか」を必ず検査する。**

集計期間が短い (≦ 4週間) と、1日の極端な配当が全体平均を歪める。例:

- 5/9-5/25 (17日間) の S+A 馬連 ROI = 151% (n=155点) ← 立派に見える
- が、5/9 単日除外で → 80% (n=145点) ← 損失層
- → 5/9 の単日 ROI = 339% (lucky day)

これを検出せず「151% の優位性」を打ち出すと、**サンプル偏り > 真の優位性** と勘違いして実装してしまう。

検査手順:
1. 期間内の日別 ROI を集計 → `python3 -c "..." | sort by ROI`
2. 単日が全 spend の >20% を占めるならフラグ
3. 単日除外時の ROI が全体平均から ±20pt 以上ぶれるなら「単日支配」と判定
4. 結論には「ただし○月X日の単日寄与が大きい (除外時ROI = Y%)」を必ず併記

**ROI 報告には日別 spend/return の表を添付すること**。これを怠ると、ユーザーが「優位性ある!」と勘違いして bet ロジックを書き換え、後で「あれ、実際は損するな」となる。

サンプルが小さい場合は **n と単日寄与の二軸で警告** する:
- n < 30 → 「サンプル不足、傾向と呼ばない」
- 単日寄与 > 25% → 「単日支配、その日を除外した値も併記」
- 両方 → 「(参考値)」のラベルで報告

### 4. Report format

Use this skeleton for the markdown output:

```markdown
# <分析タイトル> — 期間: YYYY-MM-DD 〜 YYYY-MM-DD

## TL;DR
<3行で結論>

## 仮説
<分析前に立てた仮説>

## サンプル
- 対象レース: N件
- 内訳: <信頼度別の件数表>
- 比較ベースライン: <何と何を比べたか>

## 結果

### 信頼度別 ROI
| Rating | n | 馬連 | ワイド | 三連複 | 三連単 |
|---|---|---|---|---|---|
| S | ... | ... |

### 統計的有意性
<差分pt + 母集団との比較>

## 解釈
<数字が意味すること、ただし因果は慎重に>

## 限界
- サンプル不足の bucket
- 期間バイアス (季節性、馬場差)
- 外的要因 (ペース、馬体重等は加味してない)

## 推奨アクション
<次にやるべきことを 1-3 個>
```

### 5. Anti-patterns (絶対やらない)

- "N=2 で共通点を発見!" → ❌ サンプル不足
- "1人気が飛んだ事例: ○○、××" だけで考察なし → ❌ 母集団比較必須
- 「過去X年で○○%」と書きつつ集計範囲を明示しない → ❌
- 旧ロジックとの差を測らずに「新ロジックがいい」と主張 → ❌

### 6. Commit and document

When the analysis informs a code change (e.g., new confidence threshold, new bet type weight):
- Save the report as `docs/analysis/<topic>_<date>.md`
- Reference it in the commit message of the code change
- Add to CLAUDE.md if it's a recurring insight

## Files in this skill

- `scripts/run_backtest.py` — ROI by tier × bet type with sample counts
- `scripts/find_significant_patterns.py` — Pattern discovery with baseline comparison
- `scripts/compare_versions.py` — Old vs new logic head-to-head
