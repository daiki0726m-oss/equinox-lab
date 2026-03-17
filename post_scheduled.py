"""
📬 スケジュール済みツイートを投稿
GitHub Actions から呼び出される軽量スクリプト

使い方:
  python post_scheduled.py --date 2026-03-17
  python post_scheduled.py --date 2026-03-17_pm
"""

import argparse
import json
import os
import sys

try:
    import tweepy
except ImportError:
    print("❌ tweepy が必要です: pip install tweepy")
    sys.exit(1)


def load_client():
    """環境変数からAPIクライアントを初期化"""
    api_key = os.environ.get("X_API_KEY")
    api_secret = os.environ.get("X_API_SECRET")
    access_token = os.environ.get("X_ACCESS_TOKEN")
    access_secret = os.environ.get("X_ACCESS_SECRET")

    # .env ファイルからも読み込み
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k == "X_API_KEY" and not api_key: api_key = v
                    elif k == "X_API_SECRET" and not api_secret: api_secret = v
                    elif k == "X_ACCESS_TOKEN" and not access_token: access_token = v
                    elif k == "X_ACCESS_SECRET" and not access_secret: access_secret = v

    if not all([api_key, api_secret, access_token, access_secret]):
        print("❌ X APIキーが設定されていません")
        sys.exit(1)

    return tweepy.Client(
        consumer_key=api_key, consumer_secret=api_secret,
        access_token=access_token, access_token_secret=access_secret
    )


def split_and_post(client, text):
    """280文字超えならスレッド化して投稿"""
    MAX_LEN = 270
    if len(text) <= MAX_LEN:
        chunks = [text]
    else:
        lines = text.split("\n")
        chunks, current = [], ""
        for line in lines:
            test = current + line + "\n" if current else line + "\n"
            if len(test) > MAX_LEN and current:
                chunks.append(current.strip())
                current = line + "\n"
            else:
                current = test
        if current.strip():
            chunks.append(current.strip())
        if not chunks:
            chunks = [text[:MAX_LEN]]

    parent_id = None
    for i, chunk in enumerate(chunks):
        try:
            result = client.create_tweet(
                text=chunk,
                in_reply_to_tweet_id=parent_id
            )
            tid = result.data["id"]
            parent_id = tid
            label = "ツイート" if i == 0 else f"リプライ{i}"
            print(f"✅ {label} 投稿完了 (ID: {tid}, {len(chunk)}文字)")
        except Exception as e:
            print(f"❌ 投稿失敗: {e}")
            return False
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="投稿日キー (YYYY-MM-DD or YYYY-MM-DD_pm)")
    args = parser.parse_args()

    # scheduled_tweets.json を読み込み
    json_path = os.path.join(os.path.dirname(__file__), "scheduled_tweets.json")
    if not os.path.exists(json_path):
        print(f"❌ {json_path} が見つかりません。generate_weekly_tweets.py を先に実行してください。")
        sys.exit(1)

    with open(json_path, encoding="utf-8") as f:
        tweets = json.load(f)

    if args.date not in tweets:
        print(f"⚠️ {args.date} のツイートが見つかりません")
        print(f"   登録済みの日付: {', '.join(tweets.keys())}")
        sys.exit(1)

    tweet_data = tweets[args.date]
    content = tweet_data.get("content")

    if not content:
        print(f"⚠️ {args.date} のコンテンツは事前生成されていません (type: {tweet_data['type']})")
        print("   ローカルで post_x.py を使って投稿してください。")
        sys.exit(0)

    print(f"📬 {args.date} のツイートを投稿中...")
    print(f"   タイプ: {tweet_data['type']}")
    print(f"   文字数: {len(content)}文字\n")

    client = load_client()
    success = split_and_post(client, content)

    if success:
        # 投稿済みマーク
        tweet_data["posted"] = True
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(tweets, f, ensure_ascii=False, indent=2)
        print("\n✅ 投稿完了！")


if __name__ == "__main__":
    main()
