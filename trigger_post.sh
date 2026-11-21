#!/bin/bash
# ─── cron-job.org 用のGitHub Actions 外部トリガースクリプト ───
# cron-job.orgに以下のURLをWebhookとして登録:
#   https://api.github.com/repos/daiki0726m-oss/equinox-lab/actions/workflows/auto_post_x.yml/dispatches
#
# 使い方:
#   1. GitHub Settings → Developer settings → Personal access tokens → Fine-grained token を作成
#      - Repository: equinox-lab
#      - Permissions: Actions (Read and write)
#   2. cron-job.org で無料アカウント作成
#   3. 以下のスケジュールでcron jobを登録
#
# ─── 登録するcron job一覧 ───
#
# ■ 平日
#   朝7:30(JST):  URL下記 / Body: {"ref":"main","inputs":{"mode":"morning"}}
#   昼12:00(JST): URL下記 / Body: {"ref":"main","inputs":{"mode":"weekday"}}
#   夜20:00(JST): URL下記 / Body: {"ref":"main","inputs":{"mode":"evening"}}
#
# ■ 土日（レース日）
#   朝7:00(JST):  Body: {"ref":"main","inputs":{"mode":"predict"}}
#   9:55(JST):    Body: {"ref":"main","inputs":{"mode":"odds_flash"}}
#   15:30(JST):   Body: {"ref":"main","inputs":{"mode":"hit_flash"}}
#   土曜20:00:    Body: {"ref":"main","inputs":{"mode":"answer_check"}}
#   日曜20:00:    Body: {"ref":"main","inputs":{"mode":"weekly_review"}}
#
# ─── cron-job.org の設定方法 ───
#
# URL: https://api.github.com/repos/daiki0726m-oss/equinox-lab/actions/workflows/auto_post_x.yml/dispatches
# Method: POST
# Headers:
#   Authorization: Bearer <YOUR_GITHUB_TOKEN>
#   Accept: application/vnd.github.v3+json
#   Content-Type: application/json
#
# ─── テスト: curlで手動実行 ───

GITHUB_TOKEN="${GITHUB_TRIGGER_TOKEN:-$1}"
MODE="${2:-morning}"

if [ -z "$GITHUB_TOKEN" ]; then
    echo "Usage: $0 <GITHUB_TOKEN> <MODE>"
    echo "  MODE: morning | weekday | evening | predict | odds_flash | hit_flash | answer_check | weekly_review"
    exit 1
fi

echo "📬 Triggering GitHub Actions: mode=$MODE"

curl -X POST \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  -H "Content-Type: application/json" \
  "https://api.github.com/repos/daiki0726m-oss/equinox-lab/actions/workflows/auto_post_x.yml/dispatches" \
  -d "{\"ref\":\"main\",\"inputs\":{\"mode\":\"$MODE\"}}"

echo ""
echo "✅ Triggered! Check: https://github.com/daiki0726m-oss/equinox-lab/actions"
