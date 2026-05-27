# Claude 作業ルール — EQUINOX Lab

> このファイルは Claude Code が本リポジトリで作業する際に必ず守るべき
> ルールを定義する。記事生成・投稿テンプレ作成・データ集計に関する
> ミス防止を最優先する。

## 🚨 鉄則:必ずファクトチェック

ユーザーから 2026-05-13 に「整合性チェック・ファクトチェックを必ず行う」
ように指示があった。以下を**作業終了前に必ず実行**する。

### 記事 (articles/*.html / *.md) を新規作成・編集したら

```bash
python3 scripts/verify_article.py <該当ファイル> [--race "レース名"]
```

**例**:
```bash
python3 scripts/verify_article.py \
  articles/victoria_mile_2026_note_part1.html \
  articles/victoria_mile_2026_promo_part1.md \
  --race "ヴィクトリアマイル"
```

**致命的問題 (❌)** が検出されたら commit せず修正。
**警告 (⚠️)** は内容を確認して必要なら修正。

### 検証ルール

| ルール | 検出方法 |
|---|---|
| ① 年度の連続性 | 2020-2025 中で抜けがあれば ❌ |
| ② 馬名の DB 照合 | 該当馬が `horses` テーブルに存在するか |
| ③ 数値の DB 照合 | 「○○人気・○○倍」が DB の実値と一致 |
| ④ プレースホルダー残存 | `[URL]` `[TODO]` 等が残ってないか |
| ⑤ 矛盾検出 | 同一馬の人気/オッズが文中で不整合 |

## 📝 記事作成時のチェックリスト

新規記事を書いたら以下を確認:

- [ ] 過去レース勝ち馬は**連続した年度すべて**を網羅
  - 例: 2020-2025 を扱うなら 6年分すべて記述
  - 1年でも抜けがあれば検証ツールが ❌
- [ ] 馬名・人気・オッズは DB の数値と一致
- [ ] 「歴代勝ち馬」リストは時系列(年度降順 or 昇順)で統一
- [ ] 表記揺れ無し: 「ヴィクトリアマイル」/「Victoria Mile」 等を混在させない
- [ ] note 用記事は HTML テーブルを使わない (note エディタが潰す)
- [ ] X 投稿テンプレは X:280 文字以内 + Threads:500 以内
- [ ] URL 未確定なら `[URL]` プレースホルダーを残す (テンプレ用)
- [ ] **実投稿(production)では `[URL]` 等のプレースホルダーを残さない**

## 📊 データ集計の鉄則

- 過去6年(2020-2025) を基本範囲とする
- 集計対象が3件未満なら "サンプル不足" と明記
- 単純集計でなく**クロス参照**(出走馬×コース、血統×距離 等)で深掘り
- temporal leakage に注意 (race_date < target_date のフィルタ必須)

## 🤖 投稿テンプレの3要素

すべての投稿に以下を必ず含める:

1. **具体的な馬名**(注目馬・過去勝ち馬の固有名詞)
2. **数値根拠**(なぜそう言えるかデータで示す)
3. **行動指針**(買う/見送る/次の配信告知)

「データ1行 + 結論なし」は最悪パターン (2026-05-13 にユーザー指摘済)。

## 🔧 投稿スケジュール (平日)

JRA の出走スケジュール:
- **月**: 特別登録
- **木**: 出走馬確定 + 斤量 + 馬番
- **金 11:00**: 枠順抽選
- **土・日**: レース当日

これを踏まえた slot 配置:

| 曜日 | 朝 7:30 | 昼 12:30 | 夜 20:00 |
|---|---|---|---|
| 月 | 週末ラインナップ | レース#1コース傾向 | レース#1 AI注目TOP3 |
| 火 | レース#2血統 | レース#2 AI注目TOP3 | レース#2 詳細 |
| 水 | 🏇 騎手×コース | 追い切り情報 | 危険な人気馬 |
| 木 | 出走確定+血統 | 8軸最終TOP4 | 最終3頭+note |
| 金 | 🔮 AI独自パターン分析 | 🎯 注目馬3頭と根拠のみ | 🔔 翌朝配信告知 |

「金朝に枠順発表」「金昼に買い目フル公開」は **NG**
(枠順は11時抽選 / 買い目は土朝に集約)。

## 💻 開発時のルール

- DB の write は必ず `git checkout -- keiba.db` で取り消し可能 (= **DB に補完作業中は git checkout NG。書き込み完了まで pull/checkout 禁止**)
- features.py と fast_train.py の `get_feature_columns()` は**完全一致**
- 投稿履歴: `docs/data/.post_history.json` が git tracked / source of truth。ローカルの `.post_history.json` は .gitignore 対象のフォールバックバックアップ
- weekly_retrain が走ると models/*.pkl が更新される(自動デプロイ)
- ML 特徴量を変えたら **必ず retrain が必要** (古い models/*.pkl は新 features で predict できずエラー)
- confidence.py は **v4 で ROI 期待値ベース** に再設計 (2026-05-25)。trio_ev/odds_pot/umaren_ev/top3/conc の 5軸。旧 NORMS は廃止
- 結果スレッド投稿の重複検出は **「集計ツイート」(`集計` + `◎の戦績`/`印馬の` を含む)** で判定 (PR #88)。tweets[0] は固定 header / tweets[1] は別レース構成で誤判定リスクあり、フォールバックは廃止 (2026-05-25)
- 血統データは horses テーブル全体で 60%+ 欠落しがち → `scripts/auto_pedigree.py` (cron で自動補完) を整備済

## 🐛 過去のミス事例 (繰り返さない)

### 🚨 教訓 TOP5 (これ守れば類似事故 90% 防げる)

1. **予測ロジック変更時は seal を NULL に戻して再 predict** (#12, #17 関連)
   ```sql
   UPDATE predictions_cache SET posted_at = NULL
   WHERE race_id IN (SELECT race_id FROM races WHERE race_date='YYYY-MM-DD');
   ```
2. **GitHub Actions では cache restore 直後に git の DB を強制復元** (#19)
   ```yaml
   - name: Force git DB (override stale cache)
     run: git checkout HEAD -- keiba.db
   ```
3. **X 投稿コマンドには時間ガード必須**(post_predict / odds_flash etc, #15)
4. **データパイプライン変更時は preflight_check.py で完整性確認** (#13, #14, #18)
   ```bash
   python3 scripts/preflight_check.py YYYYMMDD --auto-fix
   ```
5. **結果系投稿は「投稿対象レース全完走」が条件** (post_predict 時の seal を完走確認の trigger に)

---

### 📊 データパイプライン関連 (収集 / 同期 / cache)

- **#11 (2026-05-16)**: 血統補完中に `git checkout -- keiba.db` で補完分消失 → **DB 書込中は git pull/checkout 禁止**
- **#13 (2026-05-23)**: `fetch_weekend_races` cron が月水金朝のみで土日朝発火せず → R1-R8 欠落で結果反映停止 → **土日朝7時(UTC 22時)発火 cron に統合** (PR #92)
- **#14 (2026-05-23)**: `scrape_shutuba` は `entries` キー、`save_race_to_db` は `results` キーで6ヶ月以上未来レース出走馬保存されず → **両キー受付に修正**
- **#18 (2026-05-24)**: パイプライン全段階で完整性チェック無く変な出力が垂れ流し → **`scripts/preflight_check.py` 新設** (races件数 / 出走馬 / 予測キャッシュを auto-fix)
- **#19 (2026-05-24)**: 各 workflow が `actions/checkout` 直後に `actions/cache restore` で keiba.db を **cache 版で強制上書き**。git push した正しい DB が反映されず古い予測 (◎エンネ) が UI に → **全 9 workflow に `git checkout HEAD -- keiba.db` step 追加**

### 🤖 ML / 予測ロジック関連

- **#4 (2026-05-09)**: `combo_top3` で temporal leakage の偽 importance → **v6 で時系列累積化**
- **#6 (2026-05-16)**: ML から popularity 完全削除 → バックテスト ROI -7pt → **当日 revert** (PR #67)。教訓: 「市場の集合知を捨てるな」
- **#10 (2026-05-16)**: 信頼度 S < A の逆転 (S:27.8% / A:35.3%) → AI 過信・少頭数バイアス・クラス偏り → **WEIGHTS で pop_score 0.30、post-calibrate 12%超過60%圧縮** (PR #64)
- **#12 (2026-05-16)**: 予測ロジック変更しても DB の seal で古いまま → 明日朝 cron で古い予想 (◎17人気) 投稿寸前。dry-run で発見 → **「予測ロジック変更 = seal NULL + 再 predict 必須」**
- **#16 (2026-05-23)**: `should_bet_race` の閾値 30% が post_calibrate v8 後の分布に追従せず買い目0点 → **閾値緩和** (30%→23%, top_prob 10%→8%)
- **#17 (2026-05-24)**: Contrarian 補正が ML 識別不能レースで暴走、18人気馬が ◎ に → **ML 勝率レンジで強度可変化** (レンジ<3%停止、<5%で30%、それ以外100%)
- **#20 (2026-05-24)**: **旧 印 × 注 が「機能不全」(単独 ROI 50-69%)** → 3-5月 7,000R バックテストで判明、旧 × 注 ロジックは単独 ROI が市場標準を下回りノイズ印化していた。**穴馬スコア(SI/人気 + コース適性 + 血統 + 穴予兆 + 騎手)を新設して × 注 のみ置換** (改修済み、現在は機能良好): 単独単勝ROI ×50%→95% / 注69%→136%、 **◎軸三連複ROI 130%→174%** に劇的改善。◎○▲△ は現状ロジック維持(◎軸三連複の構造強度を保つため)。
- **#21 (2026-05-25)**: v4 信頼度 (ROI 期待値ベース) 投入後の整合性監査で **致命的不整合 5件 + 警告 7件** を発見。修正済み: (1) `evaluate_from_horses()` に `odds_key` 追加 / (2) `generate_note.py` 非 cached path を v4 化 / (3) `app.py` ダッシュボードを v4 化 / (4) `cmd_predict` を `should_bet=1 AND confidence in (S,A)` 化 / (5) `cmd_morning`/`cmd_weekday`/`cmd_evening` に時間ガード追加 / (6) tweets[1] フォールバック削除 (旧バグ復活リスク) / (7) preflight_check を manual_predict/race_day_runner にも組込 / (8) seed_historical.yml cache key を run_number に統一。**教訓: 大規模ロジック変更時は呼び出し元全箇所を grep して影響範囲を網羅すること**。

### 📝 記事 / X 投稿関連

- **#1 (2026-05-12)**: 「中身なし投稿」放置 → **v8 抜本リデザイン** (PR #40-44)
- **#2 (2026-05-13)**: 「金曜朝=枠順発表」と誤定義(実際は11時抽選) → **金朝はAI独自パターン分析に変更**
- **#3 (2026-05-13)**: ヴィクトリアマイル 2023 ソングラン抜け → **`verify_article.py` 新設**(年度連続性チェック)
- **#5 (2026-05-10)**: 「[7-1-1]」を「連勝中」と誤読(競馬慣習は着度数) → **「7着→1着→1着」表記に統一**
- **#7 (2026-05-16)**: 血統データ 66%欠落で 8軸スコア過小評価 (◎エンブロイダリー漏れ) → **`scripts/auto_pedigree.py` + cron 自動補完**
- **#8 (2026-05-16)**: post_predict の印表記「◎{馬名}」で馬番なし fact_check ブロック → **「◎ {番}番 {馬名}」形式統一** (PR #60)
- **#9 (2026-05-16)**: 結果スレッドの重複検出 tweets[0] 先頭一致で誤判定 (続編永遠に投稿不能) → **tweets[1] (2件目) で判定** (PR #69)、その後 results は集計ツイート判定に変更 (PR #88)
- **#15 (2026-05-23 16:33)**: workflow_dispatch (GAS 等外部)で post_predict 誤発火、レース真っ最中に予想投稿 → **`cmd_predict` に時間ガード追加** (11時以降スキップ)、後に odds_flash 等にも展開
- **#22 (2026-05-25)**: GitHub Actions cron が**1-4.5時間遅延発火**することが頻発 (5/25 朝 cron 7:30→8:32 = 1h遅、昼 cron 12:30→17:03 = 4.5h遅)。狭い時間ガード (例: 11:00-14:59) だと正常な遅延発火まで skip してしまう → **時間ガードを各 slot +1h 拡大** (cmd_morning 7-11 / cmd_weekday 11-15 / cmd_evening 19-23)。ただし 4.5h 遅延級は依然 skip (誤投稿事故防止優先)。教訓: 「cron は ベストエフォート、時間ガード窓は cron 遅延分も含めて設計」。
- **#23 (2026-05-25)**: X API `403 Forbidden` 発生 (5/25 朝 cmd_morning)。`get_users_tweets` (read) と `create_tweet` (write) が両方 403 = 一過性ではなく認証層/app tier の問題と推測。post_x.py の error logging が貧弱で原因切り分け不能だった → **詳細ロギング強化**: `e.response.text` / `e.api_errors` / `e.api_codes` を抽出して原因 (token 失効 / tier 制限 / 重複 / アカウント制限) のヒントを出力。Threads は同時刻に成功 = X 側固有の問題。**確認手順: https://developer.x.com/en/portal/dashboard で tier と直近 errors を確認**。
- **#24 (2026-05-26)**: 目黒記念2026 記事で「1番人気が3勝/4」と本文と矛盾した数字を書いた (実際は1人気は2勝、過去4年は1人気2勝+2人気1勝+4人気1勝)。**毎回同じパターンのミス (馬番抽選前表示、「印」確定前使用、内部メッセージ垂れ流し、数値矛盾) を指摘されて直す悪循環** → 「学習する機能はないのか」とユーザー指摘。**システム化** で対処: `scripts/verify_article.py` に `check_numeric_claim_consistency()` 新設。「N番人気がX勝」クレームを本文中の年度別人気列挙と自動カウント照合、不一致なら🚫ブロック。同様パターン (馬番抽選前、印確定前) も将来 verify_article.py に追加すべき。**教訓: 人間の検算 → 機械的検算へ。「気をつける」では再発するので、コードレベルで違反を不可能にする**。

### 💰 戦略 / ROI 最大化

- **#25 (2026-05-26) — W1 ROI最大化施策**: 7層 MECE 監査の結果、現状コードは confidence=C/D でも `should_bet=1` で投資されており、5/9-5/25 backtest で C 三連複◎軸 ROI 46%、D 31% (損失層) と確認。Δ ROI = +22.3pt 改善余地。**対策**: (1) `strategy/betting.py:should_bet_race` に `confidence` パラメータを追加して C/D は明示的に return False、(2) ◎オッズ妙味バンド検査 (2.0倍未満は配当妙味なし / 15倍超は◎信頼度低い大穴) を追加、(3) `predict.py:cmd_predict` で confidence 計算後に should_bet を再評価 (旧 path では confidence 不明状態で先に should_bet 判定していたため C/D も bet=1 のままだった)。バックテスト: 全体 ROI 69.1% → S+A のみ ROI 91.4% (Δ+22.3pt)。**教訓: confidence は予測の補助指標でなく投資判断の主軸。「分かるレースだけ買う」がROIの本質。出走馬血統データの完全 backfill (5/31 ダービー20頭 + 目黒16頭) も同時実施**。
- **#26 (2026-05-26) — 単日支配バイアス発見**: #25 の検証中に発見。**5/9-5/25 の S+A 馬連 ROI 151% は、5/9 単日 (ROI 339%) の lucky day に支配されていた**。5/9 除外で ROI 80% に転落 (損失層)。三連複も 91% → 65%、ワイドも 93% → 67% と同様。「短期間 backtest の絶対値 ROI は信用できない」という普遍的教訓。対策: race-data-analyst skill に **「単日支配チェック」を必須化** — 単日 spend > 20% / 除外時 ROI ±20pt 以上ぶれたら警告フラグ。2021 backfill 完了後にサンプル拡大して再評価必要。**教訓: ROI 報告には必ず日別 spend/return の表を添付。「優位性 +22pt」と書くなら「単日除外で +X pt」も併記**。
- **#27 (2026-05-27) — W2 confidence-aware bet weighting + 機械化セーフガード 3 本**: W1 で C/D 遮断・odds band 追加した上で、さらに「高信頼レースに重く賭ける」設計を導入。`strategy/betting.py:CONFIDENCE_MULTIPLIER` で `S=2.0x` (1点200円/budget2000円) / `A=1.5x` (line数拡大) / `B=1.0x` / `C/D=0.0` (二重防御)。同日に **3 つの機械化セーフガード**を追加: (1) `verify_article.py:check_horse_numbers_confirmed` — JRA金11時抽選前に記事で馬番を書くと 🚫 ブロック (post_position NULL/0 を確定シグナルに採用、出馬表時点の horse_number=出走順ではなく)、(2) `verify_article.py:check_marks_confirmed` — predictions_cache 未生成時に印 (◎○▲) 使用なら ⚠️、(3) `scripts/roi_weekly_monitor.py` — 週次 ROI 集計 + 4 週 rolling + 異常週 (ROI>200% or <30%) 自動 flag、JSON 出力 (`docs/data/roi_weekly.json`)。初回実行で 5/11 週 ROI 29.8% を「異常低」検出。**教訓: #24 の延長 — 人間の指摘 → 機械的ブロック化を徹底。「気をつける」では再発するので、verify_article で commit 前に止まる仕組みを増やす**。同時に 2021 historical races (3,456 R) を DB 取り込み済 = 6 年データ揃った。
- **#28 (2026-05-27) — 未勝利・新馬戦は confidence の予測力が逆転**: 5/9-5/25 race_class × confidence × 三連複◎軸 ROI cross-tab で発見。未勝利・新馬層: S=39% / A=38% / B=55% / C=64% / D=77% (高 confidence ほど低 ROI = **完全逆転**)。一方 1勝クラスは A=189% で正常、2勝クラスも A=234%。**ML が unknown horse を学習できておらず、未勝利戦に対する confidence 評価が無効**。対策: `volatility.py:compute_race_volatility` に race_class 検出を追加、race_name に「未勝利」「新馬」が含まれたら `conf_adjust = -2` で強制 2 段下げ (S→B / A→C / B→D)。should_bet_race の C/D 遮断と組み合わせて未勝利戦は実質投資対象外に。バックテスト: 全 confidence 投資 ROI 69.1% → 未勝利 -2 補正後 **113.5%** (Δ+44.4pt、初の 100% 超え)。spend は 60% 削減、return は 43% 削減 = 未勝利投資の純損失を遮断したと解釈。**教訓: 「ML の confidence は学習データ範囲内でのみ有効」。新馬・未勝利は経験データそのものが少ないので、ML の高信頼判定そのものが noise**。同日 dry-run で発見した穴予兆 NoneType crash (jockey_name=None) も修正。
- **#29 (2026-05-27) — 単年 historical backtest で 5/9 単日支配の影響が判明**: `scripts/historical_backtest.py` で 2025 単年 (3,451 races) 再現。**全体 ROI=48.2% (損失層)** — 5/9-5/25 backtest が示した 113% は 5/9 単日支配 (lucky day) の過大評価だった事実が確定。ただし confidence 軸の **相対順位は維持** (S=57.9% > B=47.3% > A=31.0%)。投資レース 764/3451 (22.1%)、スキップ 43.8% (主に未勝利 1554 件 → 補正で D に落として除外、正常動作)。**A 層が S/B より低 ROI なのは要調査** (特に 1勝 × A=23.9%、OP/特別 × A=33.7%)。**教訓 1**: 短期 backtest (17日) の絶対値 ROI は信用できない。年単位での検証が必要。**教訓 2**: confidence の relative order は valid だが、絶対 ROI を 100% 超えるには更なる loss layer 識別が必要。**教訓 3**: A 層の質改善が次の最大効果見込み (A 層 + B 層は volume が大きく寄与大)。結果ログは `docs/analysis/historical_backtest_2025_*.log` 参照。
- **#30 (2026-05-27) — 1勝クラス × loss layer 4本補正**: `scripts/analyze_a_tier_loss.py` で 1勝×A の 128 races (ROI 24.9%) を分解、loss を構成する 4 パターンを特定。(1) ダート 1勝戦 ROI 15.5% (n=77) — 芝 39.0% に対して大幅低、(2) 阪神 5.2%/中京 11.9%/札幌 0% — 中央場 (東京 43.9%/中山 49.6%) との明確な格差、(3) 短距離 (~1400m) 11.4% — 長距離 79.3% との対比、(4) ◎ 2.5-3.9倍 11.5% — 1.x-2.4倍 28.3% より低い妙味中間帯。**対策**: (a) `volatility.compute_race_volatility` に 1勝クラス検出を追加、ダート/ローカル/短距離 各 -1 で最大 -2 に capped (S→C, A→C で should_bet=False)、(b) `should_bet_race` に `race_info` パラメータ追加、1勝戦×◎2.5-3.9倍は明示的見送り。**設計判断**: 2勝以上/OP/特別/G1-G3 は ML が機能してるので適用外 (cross-tab で確認済)。1 つの signal でも -1 (A→B で bet 額減)、2 つ以上で -2 (skip)。**教訓: 「同じ confidence でも race_class × surface × venue × distance で ROI が劇的に違う」。confidence 軸単独では限界、cross-tab 分析が必須**。◎着順分布で「◎は 70% で 3着内」だが「相手不足 22.7%」も発見、これは将来の相手選定強化課題。
- **#31 (2026-05-27) — Walk-forward で 100%+ 安定 segment は 2 つだけ**: `scripts/walk_forward_segment.py` で 6 年 (20,009 races) 検証。**全 6 年で ROI ≥ 100% を維持できる segment は ZERO**。5/6 年安定は **2 segments のみ**: (1) 単勝 × confidence=S × 2勝クラス × 中距離 (avg 117.5%、min 54.7%)、(2) 単勝 × 函館 × ダート (avg 106.5%、min 61.0%)。両 segments とも 2023 が大穴年 (55%, 61%)。年間 ~74 races / 利益 ~1,000円。**教訓: 短期 backtest の 100%+ は ほぼ全部 lucky day or 期間バイアス。長期検証で生存する segment は極めて限定的**。
- **#32 (2026-05-27) — Phase α 完成 (100% 超え路線の 4 本柱)**: walk-forward で 100% 超え segment は限定的と判明 → プロ馬券師の手法を反映した 4 改修を実装: (1) **EV>1.2 厳格化**: 全 6 券種で `MIN_EV (1.2)` 統一 (旧 0.3-1.0)。honor_bets も EV >= 1.2 強制。(2) **Whitelist specialization**: `WHITELIST_SEGMENTS` クラス変数で walk-forward 確認の 2 segments のみ通過、`USE_WHITELIST=1` env var で起動可。(3) **馬体重変化補正** (`predict.py`): DB の weight_change を post-prediction で乗算補正 (±2: 1.0 / ±3-5: 0.97 / ±6-9: 0.90 / ±10+: 0.80)。(4) **馬場バイアス検出** (`scripts/track_bias_detector.py`): 同日 R1-R8 の post_position 別 3 着内率を集計、R9+ に内枠/外枠 ±8% 補正。実テスト: 2025-04-13 阪神 outside bias 検出済 (内 29% vs 外 58%)。**Phase α 効果計測 (2025 全 3,434 races)**: 単勝◎ ROI = baseline 96.9% / alpha 93.0% / **WHITELIST mode 113.4%** 🟢 (n=83)。**初の真の 100% 超え達成**。三連複は WHITELIST mode でも 30.2% で論外 → **単勝に絞るのが最適**。**教訓: 体重補正・馬場補正の全 race 適用は逆効果。specialization (whitelist × 単勝) が唯一の黒字路線**。
- **#33 (2026-05-27) — cmd_morning/evening が thread (list) 受け取れず TypeError**: 5/26 deploy の `805fc55` で slot_composer が `list[str]` (thread) を返すように、`b786033` で post_x.py から slot_composer を呼ぶ動線に切替。しかし `cmd_morning` / `cmd_evening` の `fact_check_tweet(tweet)` は `str` 前提のままだったため、5/27 朝の morning cron が `TypeError: expected string or bytes-like object, got 'list'` で失敗 (post_x.py L3425)。**修正**: `fact_check_tweet_or_thread(tweet_or_thread)` helper を新設、`str` → 旧関数に委譲 / `list` → 各要素を check。cmd_morning / cmd_evening を helper 経由に。**教訓: 戻り値の型を変える deploy は呼び出し元の grep が必須**。slot_composer 動線切替時に str→list 互換性を確認していなかった (system-integrity-audit skill のチェック対象になるべきパターン)。
- **#34 (2026-05-27) — 投稿 SLO 保証システム (cron 遅延・失敗の構造的解決)**: #22 (cron 1-4.5h 遅延) + #33 (TypeError) で 「ユーザーが気づくまで投稿されない」状態が続いていた。「夜の投稿なんでされないの? 解約しようかな…」のユーザー指摘で **「基本ルールを守る仕組み」を構造的に欠いている**ことが判明。**対策**: 4 コンポーネントの SLO 保証システム新設。(1) `config/posting_slo.yml` で 8 slot の expected/deadline/fallback_mode を declarative に明示、(2) `scripts/check_posting_health.py` で各 slot の status (ok/ok_late/missing/missed/future/not_today) を判定、(3) `.github/workflows/posting_watchdog.yml` を 30分 cron で動かして missing を自動 trigger、(4) `docs/data/posting_health.json` で常時可視化。**期待効果**: 今回のような事故は 30分以内に自動検知 + 自動 fallback で復旧。**教訓: 「投稿しない側」の守り (時間ガード) はあったが「投稿する側」の保証が無かった = 不完全な SLO。ルールは config に declarative に書き、code でなく仕組みで強制する**。

## 🛡 投稿前の Pre-flight Check (2026-05-24 導入)

各 X 投稿コマンド / 予測 cron / 結果反映 cron の前に以下を実行:
```bash
python3 scripts/preflight_check.py YYYYMMDD --auto-fix
```

これにより:
- races テーブルにレースが揃っているか確認 (不足なら fetch 自動再実行)
- 各レースに出走馬が登録されているか確認 (不足なら scrape_shutuba を再呼び出し)
- 予測キャッシュが揃っているか確認 (不足なら predict 自動実行)

返り値: 0=OK / 1=警告 / 2=致命的。GitHub Actions では `--auto-fix` でほぼ自動復旧。
