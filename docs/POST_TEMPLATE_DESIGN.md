# 投稿テンプレ刷新設計 v1 (2026-05-25)

> 旧 universal_fallback で「78文字・1指標だけ」の薄い投稿になっていた問題を解消し、
> 220-260字フル活用の「複合分析テンプレ」に置き換える設計ドキュメント。

## 背景

### 問題
- 月昼 78字 (`逃げ:複勝41.1%` 1行のみ)
- universal_fallback への切替頻発 (出走馬未確定だと中身なし扱い)
- CLAUDE.md「投稿3要素」(具体的馬名 / 数値根拠 / 行動指針) のうち②③ほぼ皆無

### 根本原因
- 馬名ベースの分析 (ai_spotlight_horses 等) は出走馬未確定だと使えない
- 月-水は出走馬未確定なのに、それに合うコンテンツが用意されていない
- 結果として「コース傾向の1指標」+「配信告知」だけのスカスカ投稿

### 解決方針
- **「出走馬未確定でも語れる複合データ」を充実させる**
- 歴代データ (過去6年勝ち馬/種牡馬/騎手) を主軸に
- データ availability に応じた段階的 fallback chain
- 全数値は DB クエリ結果に紐づけ (AI による捏造防止)

---

## Part 1: 15 slot マトリックス

```
        |  朝 7:30        |  昼 12:30       |  夜 20:00
--------|-----------------|-----------------|----------------
月       | 週末ラインナップ  | レース①コース    | レース① 注目要素
火       | レース②血統     | レース② 注目     | レース② 詳細
水       | 騎手×コース     | 追い切り         | 危険な人気馬
木       | 出走確定+血統   | 8軸最終TOP4     | 最終3頭+note告知
金       | AI独自パターン  | 注目馬3頭+根拠   | 翌朝配信告知
```

土日は別系統 (predict / odds_flash / hit_flash / results / refresh_dashboard)。

---

## Part 2: 各 slot のテーマと使う「複合データ」

| Slot | 主テーマ | データ複合 | DB クエリ範囲 |
|---|---|---|---|
| **月朝** | 週末ラインナップ | 歴代勝ち馬(年/人気/配当) + 1人気信頼度トレンド + メインレース2本 | `races`+`results`+`payouts` joined by race_name |
| **月昼** | レース①コース傾向 | コース×種牡馬 + コース×脚質×距離 + 異常年検出 | `races`×`results`×`horses` by venue/surface/distance |
| **月夜** | レース① 注目要素 | 末脚最速の勝率 + 上り3F閾値別の複勝 + 該当血統 | `results.last_3f` × `finish_position` × `horses.sire` |
| **火朝** | レース②血統深掘り | 種牡馬TOP3 × 母父TOP3 のクロス + このコース複勝50%超の系統 | `horses.sire` × `horses.damsire` cross |
| **火昼** | レース② 注目TOP3 | (登録あれば) 注目馬3頭 + 該当血統×コース実績 + 騎手×コース | `entries`+blood lookup |
| **火夜** | レース② 詳細 | 前走別の勝ち馬パターン + ローテーション(中3週/中4週/中9週) | 同馬 horse_id の `results` 履歴 |
| **水朝** | 騎手×コース | コース複勝率TOP騎手 + 騎手×距離 + 騎手×馬場状態 | `results` joined by jockey_id |
| **水昼** | 追い切り情報 | 追い切り評価 × 過去成績クロス | `workouts` table |
| **水夜** | 危険な人気馬 | 人気1-3で過去6年 複勝率低めの条件 + 該当馬 | `results` popularity × finish_position |
| **木朝** | 出走確定+血統 | 出走馬リスト + 血統で「このコース強い」馬 | `entries` (確定) + sire match |
| **木昼** | 8軸最終TOP4 | ML 予測 + 8軸スコア (anasanee_score 含む) | `predictions_cache` |
| **木夜** | 最終3頭+note告知 | ◎○▲ + その根拠 + 該当 note 記事URL | `predictions_cache.predictions_json` |
| **金朝** | AI独自パターン | 「特定条件で過去6年複勝60%超」の発掘 + 該当馬 | 動的集計 (条件×結果) |
| **金昼** | 注目馬3頭+根拠 | ML予測上位3頭 + 各馬の決定的理由(SI/血統/騎手) | `predictions_cache` |
| **金夜** | 翌朝配信告知 | 配信予定 + ティーザー(印1頭だけ匂わせ) | `predictions_cache` 軽量 |

---

## Part 3: 共通アーキテクチャ

```
weekday_engine.py に追加:

┌─ Data Layer ────────────────────────────────────┐
│ sec_historical_winners(conn, race_name, n=6)     │
│ sec_pop_trust_trend(conn, ...)                    │
│ sec_sire_course_cross(conn, venue, dist, ...)     │
│ sec_prev_race_pattern(conn, race_name)            │
│ sec_outlier_year(conn, race_name)                 │
│ sec_pace_decisive(conn, venue, dist)              │
│ sec_jockey_recent_form(conn, jockey_name)         │
│ sec_dangerous_favorites(conn, race)               │
│ sec_pattern_discovery(conn, race, threshold=0.6)  │
│                                                    │
│ 全関数の return 型:                                │
│   (title: str, lines: list[str], sample_n: int)    │
│   sample_n < 3 なら lines に「(参考)」付与         │
└────────────────────────────────────────────────────┘
                      ↓
┌─ Composer ──────────────────────────────────────┐
│ build_slot_post(slot, race) →                    │
│   sec_* を組み合わせて 220-260字 tweet を生成     │
│   出力: dict { theme, sections, hashtags, cta }  │
└────────────────────────────────────────────────────┘
                      ↓
┌─ Validator (post 前) ──────────────────────────┐
│ tweet_fact_check.db_fact_check(text)             │
│ → 通らなければ slot fallback or skip             │
└────────────────────────────────────────────────────┘
```

---

## Part 4: データ availability に応じた段階的 fallback

```
出走馬登録前 (月-火朝):  歴代データ + 種牡馬 + 異常値 (馬名は過去勝ち馬のみ)
出走馬登録後 (火夜-水):  ↑ + 出走馬の血統 + 騎手予定
出走馬確定後 (木以降):    ↑ + 注目馬名 + ML予測 + 8軸スコア
当日 (土日):              ↑ + リアルタイムオッズ + 確定買い目
```

各 slot は「メイン content + 補完 content + CTA」の3層で、メインが取れなければ補完にフォールバック、最低限の CTA は常に出す。

---

## Part 5: 実装順序

### Phase 0 (前提): データ backfill
- 2023年データ取得 ✅ (実行中 run=26399713116)
- 2022年データ取得 ⏳
- 2021年データ取得 ⏳

→ 揃わないと `tweet_fact_check` の「過去6年サンプル不足」で blocked

### Phase 1: sec_* 関数の実装 (5-7個)
- `sec_historical_winners()` 最優先 (月朝/月夜/火朝で共用)
- `sec_pop_trust_trend()` (月朝)
- `sec_sire_course_cross()` (月昼/火朝/火昼)
- `sec_prev_race_pattern()` (火夜)
- `sec_outlier_year()` (月夜)
- `sec_pace_decisive()` (月夜/水朝)
- `sec_jockey_recent_form()` (水朝/火昼)

各関数は独立にテスト可能 (引数: conn + race info, return: (title, lines, sample_n))。

### Phase 2: build_slot_post() を 15 slot 充実テンプレに書き換え
各 slot で sec_* を 3-4 個組み合わせ、220-260字に整形。

### Phase 3: テスト
- dry-run で 15 slot 全部生成
- 各 tweet が `tweet_fact_check.db_fact_check()` を通過するか
- 文字数 220-260 を満たすか
- (重要) サンプル不足時に「(サンプル不足)」明記が入るか

### Phase 4: 段階リリース
- 1 slot ずつ rollout
- 1週間運用してエンゲージメント測定
- 改善点を反映

---

## Part 6: 各 slot の出力例 (実装時の参考)

### 月朝 (例) — backfill 完了後の真の数値で書き換え予定
```
🏇 5/31(日) 日本ダービー(G1)
過去6年データが示す3つの軸

【勝ち馬の傾向 (2020-25)】
🏆2025 クロワデュノール(1人気/2.1倍)
🏆2024 ダノンデサイル(9人気/46.6倍) ←波乱
🏆... (backfill後に2021-2023追加)

【1番人気の信頼度】
東京芝2400m G1 過去6年: 1人気X勝/Y
→ 順当 / 波乱の比率を提示

【種牡馬TOP3 (過去6年・複勝率)】
... (sec_sire_course_cross の出力)

→ 火朝に血統深掘り🔔
#日本ダービー #AI予想
```

各 slot の具体例は実装時に sec_* 関数の出力を元に決定。サンプル不足判定で動的に「(参考)」付与。

---

## 補足: 既存資産の活用

`weekday_engine.py` の以下は流用可能:
- `get_sire_top` / `get_damsire_top` → `sec_sire_course_cross` の素材
- `get_jockey_course_top` → `sec_jockey_recent_form` の素材
- `ai_spotlight_horses` / `get_undervalued_horses` → 出走確定後の木夜/金昼で活用
- `get_dangerous_favorites` → `sec_dangerous_favorites` ベース

新規実装が必要なのは主に「歴代データ系」(`sec_historical_winners`, `sec_pop_trust_trend`, `sec_outlier_year`, `sec_pattern_discovery`)。

---

## 重要原則 (#21 教訓を踏まえて)

1. **全数値は DB クエリ結果のみ。ハードコードな数字は禁止**
2. **サンプル不足 (3件未満) は「(参考)」明記または skip**
3. **「過去N年」と書く場合、各年 100R 以上のデータが DB にあること**
4. **`tweet_fact_check.db_fact_check()` を全 tweet 投稿前に通す**
5. **AI 出力の「もっともらしい数字」は機械的に検出・ブロックする**
