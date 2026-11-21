#!/usr/bin/env python3
"""Threads long-lived アクセストークンを refresh して有効期限を約60日延長する。

Threads (Meta) の long-lived token は60日有効。発行から24時間経過後〜失効前
(60日以内) に refresh_access_token を呼ぶと、そこから更に約60日延長された
新トークンが得られる。週次 cron で呼び続ければ実質無期限に維持できる。

⚠️ 60日を過ぎて失効したトークンは refresh 不可 → ブラウザでの再認証(手動)が必要。
   そのため cron は「失効するずっと前 (毎週)」に実行するのが肝心。

使い方:
  # ローカル (.env or 環境変数の THREADS_ACCESS_TOKEN を使用)
  python3 scripts/refresh_threads_token.py
    → 成功時、新トークンを stdout の最終行に出力 (メッセージは stderr)

  # GitHub Actions
  GITHUB_OUTPUT があれば new_token / expires_days を output に書き出す。

終了コード: 0=成功 / 1=refresh失敗(失効含む) / 2=トークン未設定
"""
import os
import sys
import json
import urllib.request
import urllib.parse
import urllib.error

REFRESH_URL = "https://graph.threads.net/refresh_access_token"


def _load_token() -> str | None:
    """post_x.py:load_threads_client と同じく .env → 環境変数の順で読む。"""
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "THREADS_ACCESS_TOKEN":
                    return v.strip().strip('"').strip("'")
    return os.environ.get("THREADS_ACCESS_TOKEN")


def main() -> int:
    token = _load_token()
    if not token:
        print("❌ THREADS_ACCESS_TOKEN が未設定 (.env / 環境変数)", file=sys.stderr)
        return 2

    params = urllib.parse.urlencode({
        "grant_type": "th_refresh_token",
        "access_token": token,
    })
    url = f"{REFRESH_URL}?{params}"

    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"❌ Threads token refresh 失敗 (HTTP {e.code})", file=sys.stderr)
        print(f"   応答: {body}", file=sys.stderr)
        low = body.lower()
        if "expired" in low or "session has expired" in low or e.code in (400, 401):
            print("⚠️ トークンが失効済みの可能性。refresh は不可 → "
                  "Meta for Developers で新しい long-lived token を再発行し、"
                  "GitHub Secrets の THREADS_ACCESS_TOKEN を手動更新してください。",
                  file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"❌ Threads token refresh エラー: {e}", file=sys.stderr)
        return 1

    new_token = data.get("access_token")
    expires_in = int(data.get("expires_in", 0) or 0)
    days = expires_in // 86400

    if not new_token:
        print(f"❌ 応答に access_token がありません: {data}", file=sys.stderr)
        return 1

    print(f"✅ Threads token refresh 成功 (新しい有効期限: 約 {days} 日)", file=sys.stderr)

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        # 後続 step で gh secret set に使う。値はログに出さない。
        with open(gh_output, "a") as f:
            f.write(f"new_token={new_token}\n")
            f.write(f"expires_days={days}\n")
        print("   → GITHUB_OUTPUT に new_token を書き出しました", file=sys.stderr)

    # stdout には新トークンのみ (1行)。NEW_TOKEN=$(...) で受けられる。
    print(new_token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
