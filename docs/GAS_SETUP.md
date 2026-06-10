# GAS (Google Apps Script) 外部トリガー セットアップ手順 — 約10分

GitHub Actions の cron は不発・遅延が常態 (CLAUDE.md #22/#36/#56/#62 — 6/10 には
3系統の cron/watchdog が同一時間帯に全滅)。GAS は **Google インフラの時計**なので
故障モードが独立しており、これを足すと「GitHub と Google が同時に死なない限り投稿される」
状態になる。多重発火しても `posted_slots` の atomic lock で冪等 = 安全。

⚠️ **旧版の教訓 (#63)**: 以前の GAS は PAT をスクリプトに直書きして git に commit し、
GitHub secret scanning に自動失効させられて静かに死んだ。今回は **トークンを
Script Properties に保存**する (コードには書かない)。

---

## Step 1: GitHub トークン (Fine-grained PAT) を作る — 3分

1. https://github.com/settings/personal-access-tokens/new を開く
2. 設定:
   - **Token name**: `gas-trigger`
   - **Expiration**: 90 days (期限が来たら再発行 — カレンダーに登録推奨)
   - **Repository access**: Only select repositories → `daiki0726m-oss/equinox-lab`
   - **Permissions** → Repository permissions → **Actions: Read and write** (これだけ)
3. Generate token → 表示されたトークン (`github_pat_...`) をコピー
   (この画面を閉じると二度と見れない)

⚠️ ついでに https://github.com/settings/tokens で**古い漏洩トークンが残っていないか確認し、
あれば Revoke** すること (旧 gas_trigger.js に直書きされていたもの)。

## Step 2: GAS プロジェクトを作る — 5分

1. https://script.google.com → 「新しいプロジェクト」
2. プロジェクト名: `equinox-trigger`
3. エディタに `scripts/gas_trigger.js` の中身を全部貼り付け (Code.gs を置き換え)
4. **トークンを保存**: 左メニュー ⚙️「プロジェクトの設定」→ 下部「スクリプト プロパティ」
   → 「スクリプト プロパティを追加」
   - プロパティ: `GITHUB_TOKEN`
   - 値: Step 1 でコピーしたトークン
5. **動作テスト**: エディタ上部の関数選択で `testDispatch` を選んで「実行」
   - 初回は Google の権限許可ダイアログが出る → 許可
   - 実行ログに「✅ dispatch 成功: refresh_dashboard」と出れば OK
   - (GitHub の Actions タブに refresh_dashboard run が現れる。無害な内部処理)

## Step 3: 時刻トリガーを設定 — 1分 (コードで一括作成)

エディタ上部の関数選択で **`setupTriggers`** を選んで「実行」を1回押すだけ。
既存トリガーを全削除してから 8 個を作り直す (冪等なので何度実行してもよい)。
初回はトリガー管理スコープの許可ダイアログが出る → 許可。

作成されるトリガー (Asia/Tokyo):

| 関数 | 時刻 (毎日) | 発火対象 |
|---|---|---|
| `triggerMorning` | 7-8時 | 平日のみ (関数内で曜日判定) |
| `triggerWeekday` | 12-13時 | 〃 |
| `triggerEvening` | 20-21時 | 〃 |
| `triggerPredict` | 7-8時 | 土日のみ |
| `triggerOddsFlash` | 9-10時 | 〃 |
| `triggerPostPredict` | 10-11時 | 〃 |
| `triggerResults` | 17-18時 | 〃 |
| `triggerRefreshDashboard` | 18-19時 | 〃 |

(GAS の日タイマーは指定1時間幅の中のどこかで発火する。slot 側の時間ガードと
atomic lock が吸収するので、幅の中のいつ発火しても安全)

※ 2026-06-10 に上記手順で実構築済み (トリガー8個稼働中)。

## 完了後の防御構成 (4層・故障モード独立)

```
slot 定刻
├─ ① GitHub cron (本体 + 冗長)          ← GitHub スケジューラ
├─ ② weekday_runner / race_day_runner    ← 起動後は自前の時計 (GitHub compute)
├─ ③ GAS トリガー                        ← Google の時計
└─ ④ posting_watchdog (30分毎)           ← 最終バックストップ
全層が posted_slots の atomic lock を尊重 → 何重に発火しても投稿は1回
```

## トラブルシュート

- `dispatch 失敗 HTTP 401` → トークン失効。Step 1 で再発行 → Script Properties を更新
- `dispatch 失敗 HTTP 404` → Repository access に equinox-lab が入っているか確認
- GAS 側の失敗履歴: トリガー画面の「実行数」列、または左メニュー「実行数」で確認可能
