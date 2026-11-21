"""X API 診断スクリプト

実際に投稿はせず、認証・rate limit・token の状態を非破壊的に確認する。
GitHub Actions secrets 経由で credentials を受け取り、X API の最小読み取りを試行。

出力:
- credentials の有無
- get_me() の成否 + 詳細エラー
- rate limit 残量 (取れれば)
- 結論と推奨対処

Usage:
    python3 scripts/x_diagnose.py
"""
import os
import sys


def _detail_from_exception(e):
    """tweepy 例外から full response を抽出"""
    err_type = type(e).__name__
    parts = [f"[{err_type}]"]
    parts.append(str(e))

    if hasattr(e, 'response') and e.response is not None:
        try:
            body = e.response.text[:600]
            parts.append(f"\n  response.body: {body}")
        except Exception:
            pass
        try:
            headers = dict(e.response.headers)
            interesting = {
                k: v for k, v in headers.items()
                if k.lower().startswith('x-rate-limit') or k.lower().startswith('x-user-limit') or k.lower() == 'retry-after'
            }
            if interesting:
                parts.append(f"\n  rate_limit_headers: {interesting}")
        except Exception:
            pass
    if hasattr(e, 'api_errors') and e.api_errors:
        parts.append(f"\n  api_errors: {e.api_errors}")
    if hasattr(e, 'api_codes') and e.api_codes:
        parts.append(f"\n  api_codes: {e.api_codes}")
    if hasattr(e, 'api_messages') and e.api_messages:
        parts.append(f"\n  api_messages: {e.api_messages}")
    return " ".join(parts)


def main():
    print("=" * 64)
    print("🔬 X API 診断")
    print("=" * 64)

    api_key = os.environ.get("X_API_KEY")
    api_secret = os.environ.get("X_API_SECRET")
    access_token = os.environ.get("X_ACCESS_TOKEN")
    access_secret = os.environ.get("X_ACCESS_SECRET")
    bearer = os.environ.get("X_BEARER_TOKEN")

    print("\n── credential 検査 ──")
    print(f"  X_API_KEY:       {'✓ set (' + api_key[:8] + '…)' if api_key else '✗ MISSING'}")
    print(f"  X_API_SECRET:    {'✓ set' if api_secret else '✗ MISSING'}")
    print(f"  X_ACCESS_TOKEN:  {'✓ set (' + access_token[:12] + '…)' if access_token else '✗ MISSING'}")
    print(f"  X_ACCESS_SECRET: {'✓ set' if access_secret else '✗ MISSING'}")
    print(f"  X_BEARER_TOKEN:  {'✓ set' if bearer else '✗ MISSING'}")

    if not all([api_key, api_secret, access_token, access_secret]):
        print("\n❌ 必要な credential が不足。GitHub secrets を再設定してください。")
        return 1

    try:
        import tweepy
    except ImportError:
        print("\n❌ tweepy がインストールされていません")
        return 1

    client = tweepy.Client(
        bearer_token=bearer,
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_secret,
    )

    # ─── Test 1: get_me() — OAuth1 user-context の最小読み取り ───
    print("\n── Test 1: client.get_me() (OAuth1 user-context) ──")
    user_id = None
    me_result = None
    try:
        r = client.get_me()
        if r and r.data:
            user_id = r.data.id
            me_result = "ok"
            print(f"  ✓ user_id: {r.data.id}")
            print(f"  ✓ username: @{r.data.username}")
            print(f"  ✓ name: {r.data.name}")
        else:
            me_result = "empty"
            print(f"  ⚠️ response is empty: {r}")
    except Exception as e:
        me_result = "failed"
        print(f"  ✗ {_detail_from_exception(e)}")

    # ─── Test 2: 最小 read = get_users_tweets (max_results=5) ───
    print("\n── Test 2: client.get_users_tweets() (read scope check) ──")
    read_result = None
    if user_id:
        try:
            r2 = client.get_users_tweets(user_id, max_results=5)
            tweet_count = len(r2.data) if r2 and r2.data else 0
            read_result = "ok"
            print(f"  ✓ recent tweets fetched: {tweet_count} 件")
            if r2 and r2.data:
                latest = r2.data[0]
                print(f"  ✓ latest tweet (id={latest.id}): {latest.text[:50]}...")
        except Exception as e:
            read_result = "failed"
            print(f"  ✗ {_detail_from_exception(e)}")
    else:
        read_result = "skipped"
        print(f"  ⏭️ user_id が取れなかったのでスキップ")

    # ─── Test 3: v1.1 API rate_limit_status ───
    print("\n── Test 3: v1.1 rate_limit_status ──")
    try:
        auth = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_secret)
        v1 = tweepy.API(auth)
        st = v1.rate_limit_status()
        if 'resources' in st:
            # 重要なエンドポイントだけ抜粋
            interesting_endpoints = [
                ('users', '/users/me'),
                ('users', '/users/:source_id/following'),
                ('statuses', '/statuses/user_timeline'),
                ('account', '/account/verify_credentials'),
            ]
            shown = False
            for category, ep in interesting_endpoints:
                if category in st['resources']:
                    info = st['resources'][category].get(ep)
                    if info:
                        rem = info.get('remaining', '?')
                        lim = info.get('limit', '?')
                        print(f"  {ep}: {rem}/{lim} remaining")
                        shown = True
            if not shown:
                # 何か1つは出す
                for cat, eps in list(st['resources'].items())[:3]:
                    for ep, info in list(eps.items())[:1]:
                        print(f"  {ep}: {info.get('remaining')}/{info.get('limit')}")
                        break
    except Exception as e:
        print(f"  ⚠️ v1.1 rate_limit_status 取得失敗 (v2 のみ tier なら正常): {_detail_from_exception(e)}")

    # ─── 結論 ───
    print("\n" + "=" * 64)
    print("📋 診断結論")
    print("=" * 64)

    if me_result == "ok" and read_result == "ok":
        print("✅ X API の認証・read は正常に動作している")
        print("   → 5/25 朝の 403 は一過性の現象。次の cron で再試行で復活見込み。")
        print("   → write もおそらく正常。20:00 cron の挙動を待つ。")
        return 0
    elif me_result == "failed":
        print("❌ get_me() で失敗 = 認証層エラー (tier 制限 / token 失効 / app 制限)")
        print("   推奨対処:")
        print("   1. X Dev Portal → Keys and tokens → 'Regenerate' で token 再生成")
        print("   2. 新しい token を GitHub Secrets に再設定 (X_ACCESS_TOKEN, X_ACCESS_SECRET)")
        print("   3. App permissions が 'Read and write' になっているか確認")
        return 2
    elif me_result == "ok" and read_result == "failed":
        print("⚠️ get_me() OK だが read 拒否 = read scope か rate limit 制限")
        print("   推奨対処:")
        print("   1. X Dev Portal Usage タブで月次 read 上限 (Free=100/月) を確認")
        print("   2. App permissions を 'Read and write' に")
        print("   3. ロジック側: post_tweet の重複チェック (get_users_tweets) を頻度抑制")
        return 2
    else:
        print(f"⚠️ 予期しない結果 (me={me_result}, read={read_result})")
        print("   上記の Test ログを確認")
        return 1


if __name__ == "__main__":
    sys.exit(main())
