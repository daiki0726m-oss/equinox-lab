"""
🐦 X (Twitter) 自動投稿スクリプト
EQUINOX Lab — AI競馬予測の自動配信

使い方:
  # レース当日の予想投稿（メインレース）
  python post_x.py predict --date 20260322

  # 的中結果報告
  python post_x.py results --date 20260322

  # 平日コンテンツ（自動選択）
  python post_x.py weekday

  # テスト（投稿せずにプレビュー）
  python post_x.py predict --date 20260322 --dry-run
"""

import argparse
import sys
import os
import json
import random
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone

# JST タイムゾーン
JST = timezone(timedelta(hours=9))

# note URL
NOTE_URL = "https://note.com/equinox_lab"

def now_jst():
    """日本時間の現在時刻を返す"""
    return datetime.now(JST)

sys.path.insert(0, os.path.dirname(__file__))

from database import init_db, get_db

# X API (tweepy)
try:
    import tweepy
    HAS_TWEEPY = True
except ImportError:
    HAS_TWEEPY = False


# ─── Threads API ───

def load_threads_client():
    """Threads APIの認証情報を読み込む"""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    env_vars = {}
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip().strip('"').strip("'")

    user_id = env_vars.get("THREADS_USER_ID") or os.environ.get("THREADS_USER_ID")
    access_token = env_vars.get("THREADS_ACCESS_TOKEN") or os.environ.get("THREADS_ACCESS_TOKEN")

    if not user_id or not access_token:
        return None  # Threads未設定の場合はスキップ（エラーにしない）

    return {"user_id": user_id, "access_token": access_token}


def adapt_text_for_threads(tweets):
    """X用のスレッド（複数ツイート）をThreads用の1投稿に変換
    
    Threadsは500文字なので、X用の複数ツイートを結合できる。
    ハッシュタグはそのまま使える（Threadsもサポート）。
    """
    if isinstance(tweets, str):
        tweets = [tweets]
    
    # 複数ツイートを結合
    combined = "\n\n".join(tweets)
    
    # 500文字に収める（Threadsの制限）
    if len(combined) > 500:
        combined = combined[:497] + "..."
    
    return combined


def post_to_threads(threads_client, text, dry_run=False):
    """Threads APIで投稿する
    
    Threads Publishing APIの2ステップ:
    1. メディアコンテナを作成（POST /{user_id}/threads）
    2. 公開する（POST /{user_id}/threads_publish）
    """
    if not threads_client:
        return None
    
    user_id = threads_client["user_id"]
    access_token = threads_client["access_token"]
    
    if dry_run:
        print(f"\n🧵 Threads プレビュー ({len(text)}文字):")
        print(f"{'─'*40}")
        print(text)
        print(f"{'─'*40}")
        return "threads-dry-run"
    
    try:
        # Step 1: Create media container
        create_url = f"https://graph.threads.net/v1.0/{user_id}/threads"
        create_data = urllib.parse.urlencode({
            "media_type": "TEXT",
            "text": text,
            "access_token": access_token,
        }).encode("utf-8")
        
        req = urllib.request.Request(create_url, data=create_data, method="POST")
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            container_id = result["id"]
        
        # Step 2: Wait for processing
        time.sleep(2)
        
        # Step 3: Publish
        publish_url = f"https://graph.threads.net/v1.0/{user_id}/threads_publish"
        publish_data = urllib.parse.urlencode({
            "creation_id": container_id,
            "access_token": access_token,
        }).encode("utf-8")
        
        req = urllib.request.Request(publish_url, data=publish_data, method="POST")
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            post_id = result["id"]
        
        print(f"  ✅ Threads投稿完了 (ID: {post_id})")
        return post_id
        
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else str(e)
        print(f"  ⚠️ Threads投稿失敗: {e.code} {error_body}")
        return None
    except Exception as e:
        print(f"  ⚠️ Threads投稿失敗: {e}")
        return None


def load_x_client():
    """X APIクライアントを初期化"""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    env_vars = {}
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip().strip('"').strip("'")

    api_key = env_vars.get("X_API_KEY") or os.environ.get("X_API_KEY")
    api_secret = env_vars.get("X_API_SECRET") or os.environ.get("X_API_SECRET")
    access_token = env_vars.get("X_ACCESS_TOKEN") or os.environ.get("X_ACCESS_TOKEN")
    access_secret = env_vars.get("X_ACCESS_SECRET") or os.environ.get("X_ACCESS_SECRET")

    if not all([api_key, api_secret, access_token, access_secret]):
        print("❌ X APIキーが設定されていません")
        print("   .env ファイルに以下を設定してください:")
        print("   X_API_KEY=xxx")
        print("   X_API_SECRET=xxx")
        print("   X_ACCESS_TOKEN=xxx")
        print("   X_ACCESS_SECRET=xxx")
        return None

    bearer_token = env_vars.get("X_BEARER_TOKEN") or os.environ.get("X_BEARER_TOKEN")

    client = tweepy.Client(
        bearer_token=bearer_token,
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_secret
    )
    # 画像 upload 用に v1.1 API も attach (tweepy.Client は media_upload を持たない)
    try:
        auth = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_secret)
        v1_api = tweepy.API(auth)
        client._v1_api = v1_api  # 画像 upload に使う
    except Exception as e:
        print(f"  ℹ️ V1 API 初期化失敗(画像投稿無効): {e}")
        client._v1_api = None
    return client


def x_weighted_len(text):
    """X APIの文字数カウント（日本語/絵文字=2, ASCII=1）"""
    count = 0
    for c in text:
        count += 2 if ord(c) > 127 else 1
    return count


def _post_history_paths():
    """投稿履歴ファイルパス(優先順)

    設計 (2026-05-25 明文化):
    - docs/data/.post_history.json: git tracked (source of truth, CI が commit)
    - .post_history.json (root): .gitignore 対象 (local 作業用バックアップ)

    両方に書き込むのは、新規 clone 直後など docs/data/ が無い環境での
    フォールバック動作のため。読み込みは docs/data/ 優先で converge する。
    """
    repo_root = os.path.dirname(__file__)
    return [
        os.path.join(repo_root, "docs", "data", ".post_history.json"),  # primary
        os.path.join(repo_root, ".post_history.json"),                   # local fallback
    ]


def _load_post_history():
    """投稿履歴を読み込む。docs/data/ 配下優先、なければローカル fallback。"""
    for p in _post_history_paths():
        if os.path.exists(p):
            try:
                with open(p, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                continue
    return {}


def _save_post_history(history):
    """投稿履歴を保存。docs/data/ を primary (git source of truth)、
    ローカル fallback (.post_history.json) も同期書込で常に最新化。"""
    paths = _post_history_paths()
    primary = paths[0]
    try:
        os.makedirs(os.path.dirname(primary), exist_ok=True)
    except Exception:
        pass
    for p in paths:
        try:
            with open(p, 'w') as f:
                json.dump(history, f, indent=2)
        except Exception:
            pass


def race_recently_posted(race_id, hours=18):
    """race_id が直近 N 時間以内に投稿されているか。

    weekday/morning/evening の slot 間で同レース連発を回避するために使う。
    """
    if not race_id:
        return False
    history = _load_post_history()
    key = f"race:{race_id}"
    if key not in history:
        return False
    last_ts = history[key]
    try:
        elapsed = now_jst().timestamp() - float(last_ts)
        return elapsed < hours * 3600
    except (TypeError, ValueError):
        return False


def post_thread(client, tweets, dry_run=False, threads_client=None, race_id=None,
                image_paths=None):
    """ツイートのリスト（スレッド）をX + Threadsに投稿（重複チェック付き）

    Args:
        race_id: 任意。指定すると `race:<id>` キーで履歴に記録し、
                 同じ race_id が直近24h で投稿済みなら skip する。
        image_paths: 各ツイートに添付する画像パスのリスト(同 index で対応)。
                     例: [None, "chart1.png", None] = ツイート2にだけ画像。
                     画像は X API v1.1 で upload → media_id 取得 → create_tweet。
    """
    import hashlib

    # ─── 重複投稿チェック ───
    # 全ツイートの内容をハッシュ化（先頭だけでなく全文）
    content_key = hashlib.md5("||".join(tweets).encode()).hexdigest()[:16]

    # 投稿履歴ファイル
    history_path, local_history_path = _post_history_paths()

    if not dry_run:
        try:
            now_ts = now_jst().timestamp()
            history = _load_post_history()

            # race_id ベース dedup は廃止 (同レース別 angle の投稿は OK)。
            # 同レース短時間ベタ重複は content_key (本文ハッシュ) で十分防げる。

            # 24時間以内に同じハッシュがあればスキップ (本文重複のみ防御)
            if content_key in history:
                last_ts = history[content_key]
                elapsed = now_ts - last_ts
                if elapsed < 86400:  # 24時間
                    elapsed_min = int(elapsed / 60)
                    hrs = elapsed_min // 60
                    mins = elapsed_min % 60
                    print(f"⚠️ 重複検出: 同じ内容が{hrs}時間{mins}分前に投稿済み → スキップ")
                    print(f"  ハッシュ: {content_key}")
                    return []

            # X APIでも直近ツイートと比較（二重ガード）
            if client and HAS_TWEEPY:
                try:
                    me = client.get_me()
                    if me and me.data:
                        recent = client.get_users_tweets(
                            me.data.id, max_results=10,
                            tweet_fields=["created_at"]
                        )
                        if recent and recent.data:
                            # 結果ツイート (📊 印別着順 系) はスレッド先頭が常に
                            # 「📊 N/M AI予想 印別着順 + 📍京都11R...」で同じになる問題があった。
                            # → 京都だけ投稿した hit_flash と、全 3R+集計 を投稿する results が
                            #    tweets[0] / tweets[1] 一致で重複と誤判定されていた (PR #88 で修正)。
                            # v3 (2026-05-25): 集計ツイートが見つからない場合の tweets[1] フォールバックを
                            # 削除。tweets[1] は別レース構成で同じ内容になり得るため、旧バグ復活リスクあり。
                            # → 集計ツイート未検出時は API 重複チェック自体をスキップし、
                            #   local history dict (race:{id} キー) のみで重複判定する。
                            check_tweet = None
                            for t in tweets:
                                if "集計" in t and ("◎の戦績" in t or "印馬の" in t):
                                    check_tweet = t
                                    break
                            if check_tweet is None:
                                # 集計ツイートなし = X API 重複チェック対象外。
                                # 通常の morning/weekday/evening 投稿等はこちらに該当。
                                # local history-based dedup (content_key / race:{id}) が継続して効く。
                                pass
                            else:
                                first_chunk = check_tweet[:80].strip()
                                if len(first_chunk) >= 30:
                                    for t in recent.data:
                                        if first_chunk in t.text:
                                            print(f"⚠️ 重複検出（X API）: 同じ内容が既に投稿済み → スキップ")
                                            print(f"  既存ツイート: {t.text[:80]}...")
                                            return []
                except Exception as api_err:
                    err_type = type(api_err).__name__
                    detail = ""
                    if hasattr(api_err, 'response') and api_err.response is not None:
                        try:
                            detail = f" | response: {api_err.response.text[:300]}"
                        except Exception:
                            pass
                    print(f"  ℹ️ X API重複チェックスキップ [{err_type}]: {api_err}{detail}")

            # 投稿履歴を記録（古いエントリは削除: 7日超）
            history = {k: v for k, v in history.items()
                       if isinstance(v, (int, float)) and (now_ts - v) < 604800}
            history[content_key] = now_ts
            # race_id を指定された場合は race:<id> キーも記録
            # → 別 mode/別 hash で同じレースを 24h以内に再投稿することを防ぐ
            if race_id:
                history[f"race:{race_id}"] = now_ts

            _save_post_history(history)

        except Exception as e:
            print(f"⚠️ 重複チェックエラー（続行）: {e}")

    tweet_ids = []
    parent_id = None

    for i, text in enumerate(tweets):
        wlen = x_weighted_len(text)
        if dry_run:
            label = "ツイート" if i == 0 else f"└ リプライ{i}"
            print(f"\n📝 {label} ({len(text)}文字 / X:{wlen}):")
            print(f"{'─'*40}")
            print(text)
            print(f"{'─'*40}")
            tweet_ids.append(f"dry-run-{i}")
        else:
            if wlen > 280:
                print(f"  ⚠️ ツイート{i+1}が{wlen}文字（上限280）。短縮が必要です。")
            try:
                kwargs = {"text": text}
                if parent_id:
                    kwargs["in_reply_to_tweet_id"] = parent_id
                # 画像添付 (image_paths が指定されていれば該当 index の画像を upload)
                if image_paths and i < len(image_paths) and image_paths[i]:
                    img_path = image_paths[i]
                    v1_api = getattr(client, '_v1_api', None)
                    if v1_api and os.path.exists(img_path):
                        try:
                            media = v1_api.media_upload(img_path)
                            kwargs["media_ids"] = [media.media_id]
                            print(f"  📷 画像 upload 完了 ({img_path})")
                        except Exception as me:
                            print(f"  ⚠️ 画像 upload 失敗: {me} (text のみで投稿)")
                result = client.create_tweet(**kwargs)
                tid = result.data["id"]
                tweet_ids.append(tid)
                parent_id = tid
                print(f"  ✅ X投稿完了 (ID: {tid}, X:{wlen}文字)")
            except Exception as e:
                # 詳細ロギング (2026-05-25): tweepy 例外から full response を抽出
                # 旧: "X投稿失敗: 403 Forbidden" だけで原因不明だった
                err_type = type(e).__name__
                err_msg = str(e)
                detail = ""
                # tweepy.errors.HTTPException 系は response 属性を持つ
                if hasattr(e, 'response') and e.response is not None:
                    try:
                        body = e.response.text[:500]
                        detail = f" | response: {body}"
                    except Exception:
                        pass
                # tweepy.errors.TweepyException の api_errors / api_codes も拾う
                if hasattr(e, 'api_errors'):
                    detail += f" | api_errors: {e.api_errors}"
                if hasattr(e, 'api_codes'):
                    detail += f" | api_codes: {e.api_codes}"
                print(f"  ❌ X投稿失敗 [{err_type}]: {err_msg}{detail}")
                # 403 の特殊ヒント
                if "403" in err_msg or err_type == "Forbidden":
                    print(f"     ヒント: X API tier 制限 / token 失効 / 重複コンテンツ / アカウント制限 のいずれか")
                    print(f"     確認: https://developer.x.com/en/portal/dashboard")
                break

    # Threads にも同時投稿（複数ツイートを1投稿に結合）
    if threads_client:
        threads_text = adapt_text_for_threads(tweets)
        post_to_threads(threads_client, threads_text, dry_run=dry_run)

    return tweet_ids


def post_tweet(client, text, reply_to=None, dry_run=False, threads_client=None, race_id=None):
    """単一ツイートまたは自動分割して投稿（X + Threads同時）

    race_id を指定すると、post_thread の race-id ベース dedup を有効化する。
    """
    if isinstance(text, list):
        return post_thread(client, text, dry_run=dry_run, threads_client=threads_client, race_id=race_id)
    return post_thread(client, [text], dry_run=dry_run, threads_client=threads_client, race_id=race_id)


def _fetch_predictions_from_pages(date_str):
    """GitHub PagesのJSONから予測データを取得（ローカルDBフォールバック用）

    Actions側のDBで生成された予測をそのまま使うことで、
    ダッシュボードとX投稿の印が一致するようにする。
    """
    import requests as req
    url = f"https://raw.githubusercontent.com/daiki0726m-oss/equinox-lab/main/docs/data/predictions_{date_str}.json"
    try:
        resp = req.get(url, timeout=15)
        if resp.status_code != 200:
            print(f"  ❌ GitHub Pages JSON取得失敗: {resp.status_code}")
            return []

        data = resp.json()
        venues = data.get('venues', {})
        result = []

        for venue_name, races in venues.items():
            for race in races:
                horses = race.get('horses', [])
                # predictions_jsonの形式に変換
                preds_list = []
                for h in horses:
                    preds_list.append({
                        'horse_number': h.get('horse_number', 0),
                        'horse_name': h.get('horse_name', ''),
                        'mark': h.get('mark', ''),
                        'pred_win_pct': h.get('pred_win_pct', 0),
                        'pred_top3_pct': h.get('pred_top3_pct', 0),
                        'odds_win': h.get('odds_win', 0),
                        'popularity': h.get('popularity', 0),
                        'si_avg': h.get('si_avg', 0),
                        'jockey_name': h.get('jockey_name', ''),
                    })

                # DB行と同じキー名のdictを作成
                # v4 (2026-05-25): should_bet も伝播して投稿対象選定で利用
                race_dict = {
                    'race_id': race.get('race_id', ''),
                    'race_name': race.get('race_name', ''),
                    'venue': race.get('venue', venue_name),
                    'distance': race.get('distance', 0),
                    'surface': race.get('surface', ''),
                    'track_condition': race.get('track_condition', ''),
                    'grade': race.get('grade', ''),
                    'race_number': race.get('race_number', 0),
                    'start_time': race.get('start_time', ''),
                    'predictions_json': json.dumps(preds_list, ensure_ascii=False),
                    'all_bets_json': json.dumps(race.get('all_bets', {}), ensure_ascii=False),
                    'confidence': race.get('confidence', 'C'),
                    'should_bet': 1 if race.get('should_bet') else 0,
                }
                result.append(race_dict)

        print(f"  ✅ GitHub Pagesから{len(result)}レースの予測を取得")
        return result
    except Exception as e:
        print(f"  ❌ GitHub Pages JSON取得エラー: {e}")
        return []


# ─── レース当日: メインレース予想 ───
def cmd_predict(args):
    """レース前の買い目公開ツイート（11R + 信頼度S/A）

    🛡 時間ガード (2026-05-23 追加):
    post_predict は朝の予想公開専用。GAS や workflow_dispatch で誤発火された場合、
    レース終了後に「予想」が投稿される事故が発生した (5/23 16:33 JST)。
    対象日のメインレース(11R)の発走時刻を過ぎている場合は投稿せず即座にスキップ。
    """
    date_str = args.date
    dt = datetime.strptime(date_str, "%Y%m%d")
    weekday = ["月", "火", "水", "木", "金", "土", "日"][dt.weekday()]
    date_label = f"{dt.month}/{dt.day}({weekday})"
    date_hyphen = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    # ─── 🛡 時間ガード: レース後の post_predict は禁止 ───
    # 同日のレース で start_time が「現在時刻 - 30分」より前なら「もう走った」とみなす。
    # 11R より早い時刻のレースが1つでも既に始まっていれば、投稿はキャンセル。
    # SKIP_PREDICT_GUARD=1 で意図的にバイパス可能(テスト用)
    if not os.environ.get("SKIP_PREDICT_GUARD"):
        now = now_jst()
        if now.strftime("%Y%m%d") == date_str:
            # 当日かつ「11時 JST 以降」なら問答無用でスキップ
            # 朝の予想公開は 7:00-10:45 JST の窓に限定
            if now.hour >= 11:
                print(f"⚠️ 時間ガード発動: 現在 {now.strftime('%H:%M JST')} は 11時以降")
                print(f"   post_predict は朝の予想公開専用(7:00-10:45 JST)。")
                print(f"   レース終了後の予想投稿事故防止のためスキップします。")
                print(f"   バイパスするには環境変数 SKIP_PREDICT_GUARD=1 を設定。")
                return

    all_races = []

    # まずローカルDBから取得 (should_bet も含めて取得)
    with get_db() as conn:
        all_races = conn.execute("""
            SELECT ra.race_id, ra.race_name, ra.venue, ra.distance, ra.surface,
                   ra.track_condition, ra.grade, ra.race_number, ra.start_time,
                   pc.predictions_json, pc.all_bets_json, pc.confidence, pc.should_bet
            FROM races ra
            JOIN predictions_cache pc ON ra.race_id = pc.race_id
            WHERE (ra.race_date = ? OR ra.race_date = ?)
            ORDER BY ra.venue, ra.race_number
        """, (date_str, date_hyphen)).fetchall()

    # ローカルDBにない場合 → GitHub PagesのJSONからフォールバック取得
    if not all_races:
        print(f"📡 ローカルDBに予測なし → GitHub Pages JSONから取得...")
        all_races = _fetch_predictions_from_pages(date_str)

    if not all_races:
        print(f"❌ {date_str} の予測データがありません")
        return

    # 投稿対象: 11R(必ず) + 推奨レース(should_bet=1 かつ 信頼度S/A)(11R以外、最大3件)
    # 2026-05-25 v4.1: betting.py 12%/30% 厳格化を投稿選定にも反映。
    # should_bet=1 のみだと S/A/B 混在 → 信頼度 S/A も併せて高品質に絞る。
    target_races = []
    target_ids = set()

    # まず11Rを追加
    for race in all_races:
        if race['race_number'] == 11 and race['race_id'] not in target_ids:
            target_races.append(race)
            target_ids.add(race['race_id'])

    # 注目レース (信頼度 S/A): 11R以外、最大3件
    # 2026-05-30 fix: should_bet=1 必須を撤廃。post_predict は「投資推奨」ではなく
    # 「AI 注目馬コンテンツ」なので、信頼度 S/A を選定基準にする。
    # (従来は should_bet=1 AND S/A だったが、大頭数レースで top_prob<12% だと
    #  should_bet=0 になり、信頼度 A のレースでも投稿対象から漏れる問題があった)
    # should_bet=1 を優先し、足りなければ should_bet=0 の S/A でも埋める。
    rec_count = 0
    candidates = [
        r for r in all_races
        if r['race_id'] not in target_ids and r['confidence'] in ('S', 'A')
    ]
    # 並び: should_bet=1 を先に / 同条件なら S > A
    candidates.sort(key=lambda r: (
        0 if r.get('should_bet') == 1 else 1,
        {'S': 0, 'A': 1}.get(r['confidence'], 2),
    ))
    for race in candidates:
        target_races.append(race)
        target_ids.add(race['race_id'])
        rec_count += 1
        if rec_count >= 3:
            break

    if not target_races:
        print(f"❌ 投稿対象レースがありません")
        return

    print(f"🏇 {date_label} 投稿対象: {len(target_races)}レース")
    print(f"   (11R: {sum(1 for r in target_races if r['race_number']==11)}件 / "
          f"注目(S/A): {sum(1 for r in target_races if r['confidence'] in ('S','A') and r['race_number']!=11)}件)\n")

    # ── ツイート1: サマリー ──
    main_races = [r for r in target_races if r['race_number'] == 11]
    s_races = [r for r in target_races if r['confidence'] in ('S', 'A') and r['race_number'] != 11]

    t1 = f"🧠 AI競馬予想 {date_label}\n\n"

    # メインレースの◎
    for race in main_races:
        preds = json.loads(race['predictions_json']) if race['predictions_json'] else []
        honmei = next((p for p in preds if p.get('mark') == '◎'), None)
        if not honmei and preds:
            honmei = sorted(preds, key=lambda x: x.get('pred_win_pct', 0), reverse=True)[0]
        grade = f" [{race['grade']}]" if race['grade'] else ""
        t1 += f"📍{race['venue']}11R {race['race_name']}{grade}\n"
        if honmei:
            # 「◎ 5番 馬名」形式で fact_check の馬名/馬番検出を確実に通す
            t1 += f"  ◎ {honmei.get('horse_number',0)}番 {honmei.get('horse_name','?')}\n"

    if s_races:
        t1 += f"\n🔥 AI高信頼レース: {len(s_races)}件\n"

    t1 += f"\nAI印は🧵↓で事前公開\n"
    t1 += f"{data_credit(short=True)}\n"
    t1 += "#AI競馬 #競馬予想"

    # ── ツイート2以降: 各レースの印 ──
    bet_tweets = []
    for race in target_races:
        preds = json.loads(race['predictions_json']) if race['predictions_json'] else []

        # 印を全て取得（◎○▲△×注）
        marks = {}
        for p in preds:
            m = p.get('mark', '')
            if m and m not in marks:
                marks[m] = p
        if '◎' not in marks and preds:
            sorted_p = sorted(preds, key=lambda x: x.get('pred_win_pct', 0), reverse=True)
            mark_labels = ['◎', '○', '▲', '△', '×', '注']
            for i, mk in enumerate(mark_labels):
                if mk not in marks and i < len(sorted_p):
                    marks[mk] = sorted_p[i]

        grade = f" [{race['grade']}]" if race['grade'] else ""
        # 信頼度アルファベット表記に戻す (2026-05-24 v4)
        conf_emoji = "🔥" if race['confidence'] == 'S' else "⭐" if race['confidence'] == 'A' else "📊"
        is_main = "メイン" if race['race_number'] == 11 else ""

        t = f"{conf_emoji} {race['venue']}{race['race_number']}R {race['race_name']}{grade}\n"
        t += f"信頼度{race['confidence']} {is_main}\n\n"

        # 印（全て表示)— 「{mark} {番号}番 {馬名}」形式で fact_check に確実に通す
        for mk in ['◎', '○', '▲', '△', '×', '注']:
            p = marks.get(mk)
            if p:
                t += f"{mk} {p.get('horse_number',0)}番 {p.get('horse_name','?')}\n"

        bet_tweets.append(t)

    # ── 最終ツイート: CTA ──
    t_last = f"📊 以上 {date_label} のAI予想印です\n\n"
    t_last += f"全{len(target_races)}レースの印を\n"
    t_last += f"レース前に事前公開しています\n\n"
    t_last += "結果は夕方に速報します🎯\n\n"
    t_last += f"{data_credit()}\n"
    t_last += "#AI競馬 #競馬予想"

    # レース名ハッシュタグ（メインレース）
    for race in main_races[:2]:
        if race['grade']:
            tag = race['race_name'].replace(' ', '').replace('　', '')
            t_last += f" #{tag}"

    tweets = [t1] + bet_tweets + [t_last]

    # ファクトチェック（致命的な問題があれば投稿中止）
    for tw in tweets:
        if not fact_check_tweet(tw):
            print("🚫 ファクトチェック不合格のため投稿を中止します")
            return

    client = None
    threads_client = load_threads_client()
    if not args.dry_run:
        client = load_x_client()
        if not client:
            return

    post_thread(client, tweets, dry_run=args.dry_run, threads_client=threads_client)

    # 投稿成功 → 予測キャッシュを seal(以降 --force でも上書き不可)
    # これで結果配信時に「投稿時の予測」と異なる予測を参照する事故を防ぐ
    if not args.dry_run:
        try:
            from database import seal_predictions_for_date
            seal_predictions_for_date(date_str)
        except Exception as e:
            print(f"⚠️ seal失敗(非致命): {e}")


# ─── レース当日: 的中結果報告 ───
def _build_results_from_json(date_str):
    """公開済み JSON (docs/data/predictions_YYYYMMDD.json) から
    11R の race_data を組み立てる。
    JSON は git-tracked なので cache@v4 の race condition 影響なし。
    結果未確定 (finish が 0/None) のレースは除外。
    """
    import requests as req
    # 第一: ローカル checkout 内の最新 JSON (Actions でも main を checkout 済み)
    local_path = os.path.join("docs", "data", f"predictions_{date_str}.json")
    data = None
    if os.path.exists(local_path):
        try:
            with open(local_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            print(f"  📂 ローカル JSON 読み込み: {local_path}")
        except Exception as e:
            print(f"  ⚠️ ローカル JSON 読み込み失敗: {e}")
    # 第二: GitHub Pages から取得
    if data is None:
        url = f"https://raw.githubusercontent.com/daiki0726m-oss/equinox-lab/main/docs/data/predictions_{date_str}.json"
        try:
            r = req.get(url, timeout=15)
            if r.status_code == 200:
                data = r.json()
                print(f"  🌐 GitHub から JSON 取得")
            else:
                print(f"  ⚠️ GitHub JSON HTTP {r.status_code}")
                return []
        except Exception as e:
            print(f"  ⚠️ GitHub JSON 取得失敗: {e}")
            return []
    if not data:
        return []

    venues = data.get('venues', {}) if isinstance(data, dict) else {}
    race_data = []
    mark_order = ['◎', '○', '▲', '△', '×', '注']

    for venue_name, races in venues.items():
        for race in races:
            if race.get('race_number') != 11:
                continue
            horses = race.get('horses', [])
            # v3 (2026-05-17): ユーザー指摘「中途半端な状態で投稿しないで」への対応。
            # 旧版は any() で「1頭でも確定なら投稿」→ 「?着」が並ぶ未完成投稿が発生。
            # 新版は「全頭 finish 確定」かつ「1-3着がすべて確定」を必須とする。
            # (除外/中止が複数あるレースは confirmed=horse_count - 失格 を許容)
            finishes = [
                h.get('finish', 0) or 0 for h in horses
                if isinstance(h.get('finish'), int)
            ]
            top3_set = set(f for f in finishes if 1 <= f <= 3)
            confirmed_count = sum(1 for f in finishes if f >= 1)
            unconfirmed = sum(1 for h in horses
                              if not isinstance(h.get('finish'), int) or h.get('finish', 0) < 1)
            # 1-3着全部確定 + 未確定馬が 1 頭以下 (除外/中止許容) で初めて投稿
            if len(top3_set) < 3 or unconfirmed > 1:
                continue  # 中途半端は投稿しない

            marked = []
            for m in mark_order:
                horse = next((h for h in horses if h.get('mark') == m), None)
                if not horse:
                    continue
                fin = horse.get('finish')
                if not isinstance(fin, int) or fin < 1:
                    fin = None
                marked.append({
                    'mark': m,
                    'horse_number': horse.get('horse_number', 0),
                    'horse_name': horse.get('horse_name', ''),
                    'finish': fin,
                })

            if marked:
                race_data.append({
                    'venue': race.get('venue', venue_name),
                    'race_name': race.get('race_name', ''),
                    'grade': race.get('grade') or '',
                    'marks': marked,
                })

    return race_data


def _build_results_from_db(date_str, date_hyphen):
    """DB (predictions_cache + results) から 11R の race_data を組み立てる。
    JSON が無い場合のフォールバック。
    """
    race_data = []
    try:
        with get_db() as conn:
            races = conn.execute("""
                SELECT ra.race_id, ra.race_name, ra.venue, ra.grade,
                       pc.predictions_json
                FROM races ra
                JOIN predictions_cache pc ON ra.race_id = pc.race_id
                WHERE (ra.race_date = ? OR ra.race_date = ?)
                AND ra.race_number = 11
                ORDER BY ra.venue
            """, (date_str, date_hyphen)).fetchall()

            if not races:
                return []

            for race in races:
                # sqlite3.Row は dict 形式アクセスのみ。.get() は使えないので keys() ベースで安全に。
                row_keys = race.keys() if hasattr(race, 'keys') else []
                preds_raw = race['predictions_json'] if 'predictions_json' in row_keys else None
                preds = json.loads(preds_raw) if preds_raw else []

                race_id = race['race_id'] if 'race_id' in row_keys else None
                if not race_id:
                    continue

                finishes = conn.execute("""
                    SELECT horse_number, finish_position FROM results
                    WHERE race_id = ? AND finish_position > 0
                """, (race_id,)).fetchall()
                finish_map = {f['horse_number']: f['finish_position'] for f in finishes}
                if not finish_map:
                    continue

                mark_order = ['◎', '○', '▲', '△', '×', '注']
                marked = []
                for m in mark_order:
                    horse = next((p for p in preds if p.get('mark') == m), None)
                    if not horse:
                        continue
                    fin = finish_map.get(horse.get('horse_number', 0))
                    marked.append({
                        'mark': m,
                        'horse_number': horse.get('horse_number', 0),
                        'horse_name': horse.get('horse_name', ''),
                        'finish': fin,
                    })

                grade_val = race['grade'] if 'grade' in row_keys else ''
                race_data.append({
                    'venue': race['venue'] if 'venue' in row_keys else '',
                    'race_name': race['race_name'] if 'race_name' in row_keys else '',
                    'grade': grade_val or '',
                    'marks': marked,
                })
    except Exception as e:
        print(f"  ⚠️ DB フォールバック失敗: {e}")
        return []
    return race_data


def cmd_results(args):
    """印別着順を報告するスレッド投稿(金額情報なし)。

    各レース毎に1ツイート + 締めツイートで集計を表示。
    ◎○▲△×注 それぞれの着順 + 1-3着には絵文字。

    データソース優先順位 (root-cause-fix: cache@v4 が脆いので JSON-first):
      1. GitHub Pages の docs/data/predictions_YYYYMMDD.json (git-tracked, 永続)
      2. ローカル DB (predictions_cache + results) — ephemeral cache のフォールバック
    """
    date_str = args.date
    dt = datetime.strptime(date_str, "%Y%m%d")
    weekday = ["月", "火", "水", "木", "金", "土", "日"][dt.weekday()]
    date_label = f"{dt.month}/{dt.day}({weekday})"
    date_hyphen = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    # ── 1) JSON-first: 公開済み JSON から読む(永続・cache不要) ──
    race_data = _build_results_from_json(date_str)
    src = "JSON" if race_data else None

    # ── 2) DB fallback: JSON が無い・古い場合 ──
    if not race_data:
        race_data = _build_results_from_db(date_str, date_hyphen)
        src = "DB" if race_data else None

    if not race_data:
        print(f"❌ {date_str} の予測データがありません(JSON/DB共に)")
        return
    print(f"📥 結果ソース: {src}")

    # 結果未確定 (finish_map 空) のレースは _build_*_ 内ですでに除外済み
    if not race_data:
        print(f"❌ {date_str} の結果がまだ出ていません")
        return

    # 🛡 投稿済レース完走待ちガード (2026-05-24 修正版):
    # 朝の post_predict で X 投稿したレース全部が完走確定するまで結果投稿をスキップ。
    # 「投稿したレース」= predictions_cache.posted_at が NOT NULL のレース。
    # これにより「投稿で約束したレース全部の結果が揃ってから報告」が物理保証される。
    is_partial_ok = os.environ.get("ALLOW_PARTIAL_RESULTS", "0") == "1"
    if not is_partial_ok:
        with get_db() as conn:
            posted_rows = conn.execute("""
                SELECT pc.race_id, r.venue, r.race_number, r.race_name
                FROM predictions_cache pc JOIN races r ON pc.race_id = r.race_id
                WHERE (r.race_date = ? OR r.race_date = ?) AND pc.posted_at IS NOT NULL
                ORDER BY r.race_id
            """, (date_str, date_hyphen)).fetchall()
            posted_race_ids = [dict(r) for r in posted_rows]

            incomplete = []
            for pr in posted_race_ids:
                row = conn.execute(
                    "SELECT COUNT(*) c, SUM(CASE WHEN finish_position > 0 THEN 1 ELSE 0 END) d "
                    "FROM results WHERE race_id = ?",
                    (pr['race_id'],)
                ).fetchone()
                total = row['c'] or 0
                done = row['d'] or 0
                # 除外/中止1頭まで許容 (全頭確定が原則)
                if total == 0 or done < total - 1:
                    incomplete.append((pr, total, done))

        if posted_race_ids and incomplete:
            print(f"⚠️ 投稿対象 {len(posted_race_ids)}レース中 {len(incomplete)}レース未完走 → 投稿スキップ")
            for pr, total, done in incomplete[:5]:
                print(f"   未完走: {pr['venue']}{pr['race_number']}R {pr['race_name'][:14]} ({done}/{total})")
            print(f"   (投稿で予想した全レース確定後に一括報告する設計)")
            print(f"   バイパス: 環境変数 ALLOW_PARTIAL_RESULTS=1 を設定")
            return

    def medal(fin):
        return {1: '🏆', 2: '🥈', 3: '🥉'}.get(fin, '')

    def fmt_finish(fin):
        # v3 (2026-05-17): 「?着」表示を廃止 (中途半端表示を禁止)。
        # 上記の has_finish フィルタで「全頭確定」レースのみ通過するため、
        # ここで未確定 (0/None) になることは原則ない (除外/中止のみ)。
        return f'{fin}着' if fin else '除外'

    # 集計
    n_races = len(race_data)
    won = placed = showed = 0      # ◎の1着/2着内/3着内
    total_marked = 0
    top3_marked = 0
    for r in race_data:
        for m in r['marks']:
            total_marked += 1
            if m['finish'] and m['finish'] <= 3:
                top3_marked += 1
            if m['mark'] == '◎' and m['finish']:
                if m['finish'] == 1: won += 1
                if m['finish'] <= 2: placed += 1
                if m['finish'] <= 3: showed += 1

    # 各レース毎のツイート
    tweets = []
    for i, r in enumerate(race_data):
        is_first = (i == 0)
        is_last_race = (i == len(race_data) - 1)
        grade_label = f"({r['grade']})" if r['grade'] else ""

        t = ""
        if is_first:
            t += f"📊 {date_label} AI予想 印別着順\n\n"
        t += f"📍 {r['venue']}11R {r['race_name']}{grade_label}\n\n"

        n_in_top3 = 0
        for m in r['marks']:
            mdl = medal(m['finish'])
            # 馬名を10文字でtruncate
            name_disp = m['horse_name'][:10]
            t += f"{m['mark']} {m['horse_number']:>2}番 {name_disp:10} {fmt_finish(m['finish'])} {mdl}\n"
            if m['finish'] and m['finish'] <= 3:
                n_in_top3 += 1

        t += f"\n🎯 印{n_in_top3}頭が3着以内"
        if not is_last_race:
            t += "\n🧵続く"
        tweets.append(t)

    # 締めツイート
    t_last = f"📊 {date_label} 集計({n_races}R)\n\n"
    t_last += f"◎の戦績:\n"
    t_last += f" 🏆勝利:    {won}/{n_races}\n"
    t_last += f" 🥈連対(1-2): {placed}/{n_races}\n"
    t_last += f" 🥉複勝(1-3): {showed}/{n_races}\n"
    if total_marked > 0:
        rate = round(top3_marked * 100 / total_marked)
        t_last += f"\n印馬の3着内率: {top3_marked}/{total_marked} ({rate}%)\n"

    next_label = "来週も"
    if dt.weekday() == 5:  # 土曜
        next_label = "明日も"
    t_last += f"\n{next_label}AI予想配信🔔\n"
    t_last += "#競馬予想 #AI予想 #競馬結果"
    tweets.append(t_last)

    client = None
    threads_client = load_threads_client()
    if not args.dry_run:
        client = load_x_client()
        if not client:
            return

    post_thread(client, tweets, dry_run=args.dry_run, threads_client=threads_client)


# ─── 平日コンテンツ ───

def _resolve_slot_name(base_slot, today):
    """曜日別の slot_composer 用 slot 名を解決。

    base_slot = 'morning' / 'weekday' / 'evening' に対して、
    曜日に応じた専用 builder があれば優先 (tue_evening, wed_*, thu_*, fri_*)。

    マッピング:
    - 月(0): morning, weekday, evening (汎用)
    - 火(1): morning, weekday, tue_evening (前走パターン特化)
    - 水(2): wed_morning, wed_weekday (追い切り), wed_evening (危険な人気馬)
    - 木(3): thu_morning, thu_weekday (注目TOP3), thu_evening (最終予想)
    - 金(4): fri_morning (パターン発掘), fri_weekday (注目馬), fri_evening (告知)
    """
    dow = today.weekday() if hasattr(today, 'weekday') else 0
    table = {
        # (dow, base_slot) → 専用 slot 名
        (1, "evening"): "tue_evening",
        (2, "morning"): "wed_morning",
        (2, "weekday"): "wed_weekday",
        (2, "evening"): "wed_evening",
        (3, "morning"): "thu_morning",
        (3, "weekday"): "thu_weekday",
        (3, "evening"): "thu_evening",
        (4, "morning"): "fri_morning",
        (4, "weekday"): "fri_weekday",
        (4, "evening"): "fri_evening",
    }
    return table.get((dow, base_slot), base_slot)


def _build_weekday_post(slot, today):
    """slot='morning'|'weekday'|'evening' のツイートを生成。

    2026-05-26: 新 slot_composer (X スレッド対応、最大3 tweet) を優先試行。
    曜日別の専用 builder (tue_evening, wed_*, thu_*, fri_*) があれば自動的に切替。
    失敗時は旧 weekday_engine にフォールバック。

    Returns:
        (tweet_or_tweets, race_id) のタプル。tweet_or_tweets は str または list[str]。
        post_tweet() は list なら自動的に thread 投稿する。
    """
    today_d = today.date() if hasattr(today, 'date') else today

    # 曜日別の slot 名に解決
    resolved_slot = _resolve_slot_name(slot, today)
    if resolved_slot != slot:
        print(f"📅 曜日別 slot: {slot} → {resolved_slot}")

    # ─── 新パス: slot_composer ───
    if not os.environ.get("USE_OLD_COMPOSER"):
        try:
            from slot_composer import build_slot_post
            from tweet_fact_check import db_fact_check

            with get_db() as conn:
                # base slot から race を取得 (resolved_slot は build_slot_post 側で使う)
                slot_idx = {"morning": 0, "weekday": 1, "evening": 2}.get(slot, 0)
                race_row = get_todays_race(conn, slot=slot_idx)
                if race_row:
                    race = dict(race_row) if hasattr(race_row, 'keys') else race_row
                    if isinstance(race, dict) and race.get("race_name"):
                        tweets, meta = build_slot_post(resolved_slot, race, conn)
                        # tweets は list[str]。1要素なら通常投稿、複数ならスレッド投稿。
                        # ファクトチェックは全 tweet で実行 (1つでも失敗で全体ブロック)
                        all_passed = True
                        for tw in (tweets if isinstance(tweets, list) else [tweets]):
                            passed, issues = db_fact_check(tw)
                            if not passed:
                                all_passed = False
                                print(f"⚠️ ファクトチェック失敗:")
                                for i in issues:
                                    print(f"   {i}")
                                break
                        if all_passed:
                            n_tweets = len(tweets) if isinstance(tweets, list) else 1
                            print(f"✅ slot_composer 採用 (x_len={meta['char_count']}, tweets={n_tweets}, sections={meta['sections_used']})")
                            return tweets, race.get("race_id")
                        else:
                            print(f"⚠️ slot_composer 出力が fact_check ブロック → 旧パスへ")
        except Exception as e:
            print(f"⚠️ slot_composer エラー (旧パスへフォールバック): {e}")
            import traceback
            traceback.print_exc()

    # ─── 旧パス: weekday_engine (fallback) ───
    try:
        from weekday_engine import build_post_for_slot
    except Exception as e:
        print(f"⚠️ weekday_engine インポート失敗: {e}")
        return None, None

    try:
        from course_stats import get_course_stats
    except Exception:
        get_course_stats = lambda v, s, d: None  # noqa: E731

    try:
        with get_db() as conn:
            result = build_post_for_slot(
                slot=slot,
                today_d=today_d,
                conn=conn,
                get_todays_race_fn=get_todays_race,
                get_course_stats_fn=get_course_stats,
                get_entry_jockeys_fn=get_entry_jockeys,
                hashtags_fn=make_race_hashtags,
                jockey_filter_fn=filter_jockey_stats,
                return_race=True,
            )
        if isinstance(result, tuple) and len(result) == 2:
            return result
        return result, None
    except Exception as e:
        print(f"⚠️ {slot} 投稿エラー: {e}")
        import traceback
        traceback.print_exc()
        return None, None


def cmd_weekday(args):
    """平日昼: 今週末メインレースのコース分析（曜日別テーマ・日付動的表現）

    🛡 時間ガード (2026-05-25 追加 / 同日 +1h 拡大):
    cmd_weekday は 11:00-15:59 JST 専用。GitHub Actions cron が
    1-3h 遅延発火するケースは正常運転として許容するが、夕方以降は
    昼コンテンツとして不適切なのでスキップ (5/25 は 17:03 発火 → 正しくスキップ)。
    SKIP_TIME_GUARD=1 で意図的にバイパス可能(テスト用)。
    """
    if not os.environ.get("SKIP_TIME_GUARD"):
        now = now_jst()
        if not (11 <= now.hour <= 15):
            print(f"⚠️ cmd_weekday 時間ガード: {now.strftime('%H:%M JST')} は対象外 (11:00-15:59 のみ)")
            print(f"   バイパスするには環境変数 SKIP_TIME_GUARD=1 を設定。")
            return

    fetch_weekend_races()
    today = now_jst()
    dow = today.weekday()
    print(f"📅 昼投稿 JST曜日: {['月','火','水','木','金','土','日'][dow]}曜日")

    tweet, race_id = _build_weekday_post('weekday', today)

    if not tweet:
        print("⚠️ 投稿コンテンツを生成できませんでした → 投稿スキップ")
        return

    client = None
    threads_client = load_threads_client()
    if not args.dry_run:
        client = load_x_client()
        if not client:
            return

    post_tweet(client, tweet, dry_run=args.dry_run, threads_client=threads_client, race_id=race_id)


def generate_weekly_summary():
    """月曜: 先週末11Rの的中結果（3ツイート）"""
    today = now_jst()
    last_sun = today - timedelta(days=today.weekday() + 1)
    last_sat = last_sun - timedelta(days=1)

    sat_str = last_sat.strftime("%Y-%m-%d")
    sun_str = last_sun.strftime("%Y-%m-%d")
    dr = f"{last_sat.month}/{last_sat.day}-{last_sun.month}/{last_sun.day}"

    try:
        with get_db() as conn:
            # 先週末11Rの予測と結果を照合
            races_11r = conn.execute("""
                SELECT ra.race_id, ra.race_name, ra.venue, ra.race_date,
                       pc.predictions_json
                FROM races ra
                JOIN predictions_cache pc ON ra.race_id = pc.race_id
                WHERE ra.race_date IN (?, ?)
                AND ra.race_number = 11
                ORDER BY ra.race_date, ra.venue
            """, (sat_str, sun_str)).fetchall()

            results_list = []
            for race in races_11r:
                preds = json.loads(race['predictions_json']) if race['predictions_json'] else []
                # AI◎の馬（1位）の着順を取得
                if preds:
                    top_horse = preds[0]
                    horse_num = top_horse.get('horse_number', 0)
                    # 実際の着順取得
                    actual = conn.execute("""
                        SELECT r.finish_position, r.odds FROM results r
                        WHERE r.race_id = ? AND r.horse_number = ?
                        AND r.finish_position > 0
                    """, (race['race_id'], horse_num)).fetchone()

                    results_list.append({
                        'venue': race['venue'],
                        'race_name': race['race_name'],
                        'horse_name': top_horse.get('horse_name', '?'),
                        'finish': actual['finish_position'] if actual else '?',
                        'odds': actual['odds'] if actual else 0,
                        'mark': top_horse.get('mark', '◎'),
                    })
    except:
        results_list = []

    if not results_list:
        # データがない場合はコラムに切替
        return None  # データ不足時はスキップ

    # 的中数計算（3着以内を的中とする）
    hits = sum(1 for r in results_list if isinstance(r['finish'], int) and r['finish'] <= 3)
    total = len(results_list)
    hit_rate = round(hits / total * 100) if total > 0 else 0

    t1 = f"📊 先週末({dr}) AI予測の結果\n"
    t1 += f"対象: メインレース(11R) {total}レース\n\n"
    t1 += f"AI本命(◎)の複勝的中率: {hits}/{total} ({hit_rate}%)\n\n"
    t1 += f"{data_credit(short=True)}\n"
    t1 += "#競馬予想 #AI予想 🧵↓"

    t2 = "📋 各レース結果\n\n"
    for r in results_list:
        if isinstance(r['finish'], int) and r['finish'] <= 3:
            t2 += f"✅ {r['venue']} {r['race_name']}\n"
            t2 += f" {r['horse_name']} → {r['finish']}着\n"
        else:
            pos = r['finish'] if r['finish'] != '?' else '?'
            t2 += f"❌ {r['venue']} {r['race_name']}\n"
            t2 += f" {r['horse_name']} → {pos}着\n"

    t3 = "💡 来週に向けて\n\n"
    if hit_rate >= 50:
        t3 += f"複勝的中率{hit_rate}%は好調\n"
        t3 += "引き続きデータを蓄積していきます\n\n"
    else:
        t3 += "的中率は改善の余地あり\n"
        t3 += "モデルの精度向上に取り組みます\n\n"
    t3 += "土日朝8時にメインレースAI予想を配信\n"
    t3 += "フォロー&通知ONで見逃さない🔔"

    return [t1, t2, t3]


def generate_jockey_ranking():
    """火曜: データ系ローテーション（4週サイクル）"""
    today = now_jst()
    week_num = today.isocalendar()[1] % 4  # 0-3でローテーション

    if week_num == 0:
        return _generate_trainer_ranking()
    elif week_num == 1:
        return _generate_jt_combo()
    elif week_num == 2:
        return _generate_course_analysis()
    else:
        return _generate_distance_specialty()


def _generate_trainer_ranking():
    """調教師ランキング"""
    today = now_jst()
    start_date = today - timedelta(days=30)

    with get_db() as conn:
        last_race = conn.execute("""
            SELECT MAX(ra.race_date) as d FROM races ra
            JOIN results r ON ra.race_id = r.race_id
            WHERE r.finish_position > 0 AND ra.race_date <= date('now')
        """).fetchone()
        end_dt = datetime.strptime(last_race['d'], "%Y-%m-%d") if last_race and last_race['d'] else today

        trainers = conn.execute("""
            SELECT t.trainer_name,
                   COUNT(*) as entries,
                   SUM(CASE WHEN r.finish_position = 1 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN r.finish_position <= 3 THEN 1 ELSE 0 END) as top3
            FROM results r
            JOIN trainers t ON r.trainer_id = t.trainer_id
            JOIN races ra ON r.race_id = ra.race_id
            WHERE ra.race_date >= date('now', '-30 days')
            AND ra.race_date <= date('now')
            AND r.finish_position > 0
            GROUP BY t.trainer_id
            HAVING entries >= 10
            ORDER BY CAST(wins AS FLOAT)/entries DESC
            LIMIT 5
        """).fetchall()

    if not trainers:
        return None  # データ不足時はスキップ

    period = f"{start_date.year}/{start_date.month}/{start_date.day}〜{end_dt.year}/{end_dt.month}/{end_dt.day}"

    t1 = f"🏆 調教師 勝率ランキング\n"
    t1 += f"集計期間: {period}\n\n"
    t1 += "勝率が高い=仕上げ力がある厩舎\n"
    t1 += "10頭以上出走の調教師を集計\n\n"
    t1 += "📊直近30日のトレンド分析\n"
    t1 += "#競馬予想 #AI予想 🧵↓"

    medals = ["🥇", "🥈", "🥉", " 4.", " 5."]
    t2 = f"📊 勝率ランキング({period})\n\n"
    for i, t in enumerate(trainers):
        win_rate = round(t['wins']/t['entries']*100, 1)
        top3_rate = round(t['top3']/t['entries']*100, 1)
        t2 += f"{medals[i]}{t['trainer_name']}\n"
        t2 += f"  勝率{win_rate}% 複勝率{top3_rate}%({t['entries']}頭)\n"

    t3 = "💡 馬券に活かすポイント\n\n"
    t3 += "勝率が高い厩舎は仕上げが上手い\n"
    t3 += "特に休み明けの馬に注目\n\n"
    t3 += "AIは騎手×調教師の相性も\n"
    t3 += "モデルに組み込んでいます🧠"

    return [t1, t2, t3]


def _generate_jt_combo():
    """騎手×調教師コンビ好成績"""
    today = now_jst()
    start_date = today - timedelta(days=90)

    with get_db() as conn:
        last_race = conn.execute("""
            SELECT MAX(ra.race_date) as d FROM races ra
            JOIN results r ON ra.race_id = r.race_id
            WHERE r.finish_position > 0 AND ra.race_date <= date('now')
        """).fetchone()
        end_dt = datetime.strptime(last_race['d'], "%Y-%m-%d") if last_race and last_race['d'] else today

        combos = conn.execute("""
            SELECT j.jockey_name, t.trainer_name,
                   COUNT(*) as rides,
                   SUM(CASE WHEN r.finish_position = 1 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN r.finish_position <= 3 THEN 1 ELSE 0 END) as top3
            FROM results r
            JOIN jockeys j ON r.jockey_id = j.jockey_id
            JOIN trainers t ON r.trainer_id = t.trainer_id
            JOIN races ra ON r.race_id = ra.race_id
            WHERE ra.race_date >= date('now', '-90 days')
            AND ra.race_date <= date('now')
            AND r.finish_position > 0
            GROUP BY r.jockey_id, r.trainer_id
            HAVING rides >= 5
            ORDER BY CAST(top3 AS FLOAT)/rides DESC
            LIMIT 5
        """).fetchall()

    if not combos:
        return None  # データ不足時はスキップ

    period = f"{start_date.year}/{start_date.month}/{start_date.day}〜{end_dt.year}/{end_dt.month}/{end_dt.day}"

    t1 = "🤝 騎手×調教師 好相性コンビTOP5\n"
    t1 += f"集計期間: {period}\n\n"
    t1 += "同じ騎手でも調教師との相性で\n"
    t1 += "成績が大きく変わる\n\n"
    t1 += "📊直近90日のトレンド分析\n"
    t1 += "#競馬予想 #AI予想 🧵↓"

    t2 = f"📊 複勝率が高いコンビ\n\n"
    for i, c in enumerate(combos, 1):
        jname = c['jockey_name'].lstrip('▲△★☆')
        rate = round(c['top3']/c['rides']*100)
        t2 += f"{i}. {jname}×{c['trainer_name']}\n"
        t2 += f"  複勝率{rate}% ({c['top3']}/{c['rides']})\n"

    t3 = "💡 コンビ力の見方\n\n"
    t3 += "好相性コンビの馬が出走したら\n"
    t3 += "人気がなくても要注意\n\n"
    t3 += "AIの最重要特徴量がこの\n"
    t3 += "騎手×調教師コンビの実績です🧠"

    return [t1, t2, t3]


def _generate_course_analysis():
    """コース別成績分析"""
    today = now_jst()

    # 今週末の開催場を取得
    with get_db() as conn:
        venues = conn.execute("""
            SELECT DISTINCT venue FROM races
            WHERE race_date > date('now') AND race_date <= date('now', '+7 days')
        """).fetchall()

        if not venues:
            return None  # データ不足時はスキップ

        venue_name = venues[0]['venue']

        # そのコースでの枠番別成績
        frame_data = conn.execute("""
            SELECT
                CASE WHEN r.horse_number <= 4 THEN '内枠(1-4)'
                     WHEN r.horse_number <= 8 THEN '中枠(5-8)'
                     ELSE '外枠(9-)' END as frame_group,
                COUNT(*) as runs,
                SUM(CASE WHEN r.finish_position <= 3 THEN 1 ELSE 0 END) as top3
            FROM results r
            JOIN races ra ON r.race_id = ra.race_id
            WHERE ra.venue = ?
            AND ra.race_date >= date('now', '-90 days')
            AND ra.race_date <= date('now')
            AND r.finish_position > 0
            GROUP BY frame_group
            ORDER BY frame_group
        """, (venue_name,)).fetchall()

        # 脚質別成績
        pace_data = conn.execute("""
            SELECT
                CASE WHEN r.passing_order LIKE '1-%' OR r.passing_order LIKE '2-%' THEN '先行'
                     ELSE '差し・追込' END as style,
                COUNT(*) as runs,
                SUM(CASE WHEN r.finish_position <= 3 THEN 1 ELSE 0 END) as top3
            FROM results r
            JOIN races ra ON r.race_id = ra.race_id
            WHERE ra.venue = ?
            AND ra.race_date >= date('now', '-90 days')
            AND ra.race_date <= date('now')
            AND r.finish_position > 0
            AND r.passing_order != ''
            GROUP BY style
        """, (venue_name,)).fetchall()

    t1 = f"🏟️ {venue_name}コース傾向分析\n"
    t1 += f"直近90日のデータから\n\n"
    t1 += f"今週末の{venue_name}開催に向けて\n"
    t1 += "コースバイアスをチェック\n\n"
    t1 += "📊直近90日のトレンド分析\n"
    t1 += "#競馬予想 #AI予想 🧵↓"

    t2 = f"📊 {venue_name}の傾向\n\n"
    if frame_data:
        t2 += "【枠番別複勝率】\n"
        for f in frame_data:
            rate = round(f['top3']/f['runs']*100, 1) if f['runs'] > 0 else 0
            t2 += f"・{f['frame_group']}: {rate}%({f['runs']}走)\n"
    if pace_data:
        t2 += "\n【脚質別複勝率】\n"
        for p in pace_data:
            rate = round(p['top3']/p['runs']*100, 1) if p['runs'] > 0 else 0
            t2 += f"・{p['style']}: {rate}%({p['runs']}走)\n"

    t3 = "💡 馬券に活かすポイント\n\n"
    t3 += f"{venue_name}のバイアスを把握して\n"
    t3 += "有利な条件の馬を狙うのが基本\n\n"
    t3 += "AIもコース傾向を加味して\n"
    t3 += "予測しています🧠"

    return [t1, t2, t3]


def _generate_distance_specialty():
    """距離替わり成功率データ"""
    with get_db() as conn:
        # 距離短縮/延長時の成績
        dist_data = conn.execute("""
            WITH race_pairs AS (
                SELECT r.horse_id, ra.distance as dist,
                       LAG(ra.distance) OVER (PARTITION BY r.horse_id ORDER BY ra.race_date) as prev_dist,
                       r.finish_position
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE r.finish_position > 0
                AND ra.race_date >= date('now', '-180 days')
                AND ra.race_date <= date('now')
            )
            SELECT
                CASE WHEN dist > prev_dist THEN '距離延長'
                     WHEN dist < prev_dist THEN '距離短縮'
                     ELSE '同距離' END as change_type,
                COUNT(*) as runs,
                SUM(CASE WHEN finish_position <= 3 THEN 1 ELSE 0 END) as top3
            FROM race_pairs
            WHERE prev_dist IS NOT NULL
            GROUP BY change_type
            ORDER BY change_type
        """).fetchall()

    if not dist_data:
        return None  # データ不足時はスキップ

    t1 = "📏 距離変更と成績の関係\n\n"
    t1 += "距離を延長/短縮した馬は\n"
    t1 += "成績にどう影響する？\n"
    t1 += "半年分のデータで検証\n\n"
    t1 += "📊直近180日のトレンド分析\n"
    t1 += "#競馬予想 #AI予想 🧵↓"

    t2 = "📊 距離変更別の複勝率\n\n"
    for d in dist_data:
        rate = round(d['top3']/d['runs']*100, 1) if d['runs'] > 0 else 0
        t2 += f"・{d['change_type']}: 複勝率{rate}%({d['runs']}走)\n"
    t2 += "\n※ 直近180日の全レース対象"

    t3 = "💡 馬券に活かすポイント\n\n"
    t3 += "距離変更は重要なファクター\n"
    t3 += "木曜の注目馬でも\n"
    t3 += "条件替わりの馬をピックアップ中\n\n"
    t3 += "AIは過去の距離別成績を\n"
    t3 += "個別に評価しています🧠"

    return [t1, t2, t3]


# ─── 共通ヘルパー: 今週末の重賞データ取得 ───────────────────

# G1レース名リスト
G1_RACES = [
    '桜花賞', '皐月賞', '天皇賞', '日本ダービー', 'オークス', '宝塚記念',
    '菊花賞', '秋華賞', 'エリザベス女王杯', 'マイルチャンピオンシップ', 'マイルCS',
    'ジャパンカップ', 'ジャパンC', '有馬記念', '高松宮記念', 'フェブラリーS',
    'フェブラリーステークス', '大阪杯', 'NHKマイルC', 'NHKマイルカップ',
    'ヴィクトリアマイル', '安田記念', 'スプリンターズS', 'スプリンターズステークス',
    'チャンピオンズC', 'チャンピオンズカップ', '朝日杯', '阪神JF',
    'ホープフルS', 'ホープフルステークス',
]

# G2レース名リスト
G2_RACES = [
    '阪神牝馬S', 'ニュージーランドT', 'ニュージーランドトロフィー',
    '毎日杯', '日経賞', '阪神大賞典', 'スプリングS', 'スプリングステークス',
    '弥生賞', 'チューリップ賞', '中山記念', '京都記念', '小倉大賞典',
    '東京新聞杯', 'ダービー卿CT', 'ダービー卿チャレンジトロフィー',
    'マーチS', 'マーチステークス', 'ファルコンS', 'ファルコンステークス',
    '愛知杯', 'フィリーズレビュー', '金鯱賞', 'きさらぎ賞',
    '京王杯SC', 'セントウルS', 'オールカマー', '毎日王冠',
    '富士S', 'スワンS', 'アルゼンチン共和国杯', 'ステイヤーズS',
    '札幌記念', '新潟記念', '目黒記念', '産経大阪杯',
    '青葉賞', 'フローラS', 'セントライト記念', 'ローズS',
    '神戸新聞杯', '京都大賞典', 'デイリー杯',
]


def detect_grade(race_name, db_grade=None):
    """レース名またはDB格からG1/G2/G3/L/OPを判定

    Args:
        race_name: レース名
        db_grade: DBのgradeカラム値（G1/G2/G3/L/OP等）。あれば最優先。
    """
    # DB格が設定されていれば最優先
    if db_grade:
        # 正規化
        grade_map = {'GI': 'G1', 'GII': 'G2', 'GIII': 'G3'}
        normalized = grade_map.get(db_grade, db_grade)
        if normalized in ('G1', 'G2', 'G3', 'L', 'OP'):
            return normalized
        # 条件クラス（3勝クラス等）は空を返す
        return ''

    # フォールバック: レース名からの判定（G1/G2のみ確実に判定可能）
    for name in G1_RACES:
        if race_name.startswith(name) or race_name == name:
            return 'G1'
    for name in G2_RACES:
        if race_name.startswith(name) or race_name == name:
            return 'G2'
    # G3以下はレース名だけでは正確に判定不可のため空を返す
    return ''

def fetch_weekend_races():
    """来週末の11Rをスクレイピングし、DBに登録する"""
    from scraper import NetkeibaScraper
    import time as _time

    today = now_jst()
    dow = today.weekday()
    days_until_sat = (5 - dow) % 7
    if days_until_sat == 0 and dow != 5:
        days_until_sat = 7
    if dow == 5:
        days_until_sat = 0
    elif dow == 6:
        days_until_sat = 6

    next_sat = today + timedelta(days=days_until_sat)
    next_sun = next_sat + timedelta(days=1)

    with get_db() as conn:
        # 既に登録済みか確認
        existing = conn.execute("""
            SELECT COUNT(*) as c FROM races
            WHERE race_date BETWEEN ? AND ? AND race_number = 11
        """, (next_sat.strftime('%Y-%m-%d'), next_sun.strftime('%Y-%m-%d'))).fetchone()

        if existing['c'] > 0:
            print(f"✅ {next_sat.strftime('%m/%d')}-{next_sun.strftime('%m/%d')} のレースは登録済み({existing['c']}件)")
            return

        print(f"📥 {next_sat.strftime('%m/%d')}-{next_sun.strftime('%m/%d')} のレースを取得中...")
        scraper = NetkeibaScraper()
        registered = 0

        for target_date in [next_sat, next_sun]:
            date_str = target_date.strftime('%Y%m%d')
            try:
                race_ids = scraper.get_race_list_by_date(date_str)
                r11_ids = [rid for rid in race_ids if rid.endswith('11')]

                for rid in r11_ids:
                    data = scraper.scrape_shutuba(rid)
                    if data:
                        conn.execute("""
                            INSERT OR REPLACE INTO races
                            (race_id, race_name, race_date, venue, race_number,
                             surface, distance, grade, direction, weather,
                             track_condition, horse_count, start_time)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """, (data['race_id'], data['race_name'], data['race_date'],
                              data['venue'], data['race_number'], data['surface'],
                              data['distance'], data['grade'], data['direction'],
                              data['weather'], data['track_condition'],
                              data['horse_count'], data['start_time']))

                        # 出走馬もresultsに仮登録（horse_number, horse_id, odds, popularity等）
                        # impost(斤量)もINSERTするように修正(features.pyがdtype要求するため必須)
                        for entry in data.get('entries', []):
                            if entry.get('horse_id'):
                                conn.execute("""
                                    INSERT OR IGNORE INTO results
                                    (race_id, horse_id, horse_number, odds, popularity, post_position,
                                     finish_position, finish_time, last_3f, passing_order, weight, weight_change, impost)
                                    VALUES (?,?,?,?,?,?, 0,0,0,0,0,0,?)
                                """, (rid, entry['horse_id'], entry.get('horse_number', 0),
                                      entry.get('odds', 0), entry.get('popularity', 0),
                                      entry.get('post_position', 0),
                                      entry.get('impost', 57.0)))
                                # horsesテーブルにも登録
                                conn.execute("""
                                    INSERT OR IGNORE INTO horses (horse_id, horse_name)
                                    VALUES (?,?)
                                """, (entry['horse_id'], entry.get('horse_name', '')))

                        registered += 1
                        print(f"  ✅ {data['race_name']} ({data['venue']}{data['surface']}{data['distance']}m)")
                    _time.sleep(1)
            except Exception as e:
                print(f"  ⚠️ {date_str} スクレイピングエラー: {e}")

        conn.commit()
        print(f"✅ {registered}レース登録完了")


def get_weekend_graded_races(conn):
    """今週末の11Rを取得（なければ直近週末）。G1>G2>G3>OPでソート"""
    today = now_jst()
    dow = today.weekday()
    days_until_sat = (5 - dow) % 7
    if days_until_sat == 0 and dow != 5:
        days_until_sat = 7
    if dow == 5:
        days_until_sat = 0
    elif dow == 6:
        days_until_sat = 6

    next_sat = today + timedelta(days=days_until_sat)
    next_sun = next_sat + timedelta(days=1)
    sat_str = next_sat.strftime('%Y-%m-%d')
    sun_str = next_sun.strftime('%Y-%m-%d')

    # まず来週末のレースを検索
    result = conn.execute("""
        SELECT race_id, race_name, venue, surface, distance, grade, race_date, race_number
        FROM races
        WHERE race_date BETWEEN ? AND ? AND race_number = 11
        ORDER BY race_date, venue
    """, (sat_str, sun_str)).fetchall()

    # 来週末のレースがなければ直近の11Rを使う
    if not result:
        result = conn.execute("""
            SELECT race_id, race_name, venue, surface, distance, grade, race_date, race_number
            FROM races
            WHERE race_number = 11 AND race_date <= ?
            ORDER BY race_date DESC
            LIMIT 6
        """, (today.strftime('%Y-%m-%d'),)).fetchall()

    # sqlite3.Rowをdictに変換（.get()が使えるように）
    result = [dict(r) for r in result]

    # G1 > G2 > G3 > L > OP でソート
    grade_order = {'G1': 0, 'G2': 1, 'G3': 2, 'L': 3, 'OP': 4, '': 5}
    result = sorted(result, key=lambda r: grade_order.get(detect_grade(r['race_name'], r.get('grade')), 5))

    return result


def get_race_jockeys(conn, race_id):
    """レースの出走馬の騎手名リストを取得"""
    rows = conn.execute("""
        SELECT DISTINCT r.jockey FROM results r
        WHERE r.race_id = ?
    """, (race_id,)).fetchall()
    if rows:
        return [r['jockey'] for r in rows if r['jockey']]

    # resultsにまだデータがない場合（レース前）、scrapedデータから取得を試みる
    # race_idから出馬表の騎手を取得
    return []


def get_entry_jockeys(race):
    """出馬表ページから今回の出走馬の騎手名リストを取得"""
    try:
        import re
        import requests
        from bs4 import BeautifulSoup
        url = f"https://race.netkeiba.com/race/shutuba.html?race_id={race['race_id']}"
        resp = requests.get(url, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/120.0.0.0 Safari/537.36'
        }, timeout=15)
        resp.encoding = 'euc-jp'
        soup = BeautifulSoup(resp.text, 'lxml')
        table = soup.find('table', class_='Shutuba_Table')
        if not table:
            return []

        jockeys = []
        for tr in table.find_all('tr', class_='HorseList'):
            tds = tr.find_all('td')
            if len(tds) >= 7:
                jockey_td = tds[6]
                jockey_link = jockey_td.find('a')
                name = jockey_link.text.strip() if jockey_link else jockey_td.text.strip()
                if name and name != '○○':
                    jockeys.append(name)
        return jockeys
    except Exception as e:
        print(f"⚠️ 騎手取得エラー: {e}")
        return []


def filter_jockey_stats(jockey_stats, entry_jockeys):
    """コース騎手成績を出走馬の騎手でフィルタリング

    entry_jockeysが空の場合はそのまま返す（フォールバック）。
    騎手名は部分一致で照合（例: 'ルメール' と 'Ｃ．ルメール'）。
    """
    if not entry_jockeys:
        return jockey_stats

    filtered = []
    for j in jockey_stats:
        stat_name = j['jockey']
        for entry_name in entry_jockeys:
            # 部分一致（姓だけの場合に対応）
            if entry_name in stat_name or stat_name in entry_name:
                filtered.append(j)
                break
    return filtered if filtered else jockey_stats[:3]  # フィルタ結果が空なら上位3人


def get_main_weekend_race(conn):
    """今週末の最もグレードの高い重賞を1つ返す"""
    races = get_weekend_graded_races(conn)
    if races:
        return races[0]
    return None


def get_todays_race(conn, slot=0):
    """今日の曜日＋時間帯に応じて週末レースをローテーションで返す。

    方針: 同じレースを複数日 featured するのは OK(同レースのストーリー積み上げ)。
    重複防止は build_morning/weekday/evening_tweet が dow 別 angle で
    違う content を出すことで実現する(同じハッシュは content_key で弾く)。

    slot: 0=朝, 1=昼, 2=夜

    重賞3件: dow と slot を組合せてローテ
    重賞2件: 同上(2巡で1サイクル)
    重賞1件: 全て同レース(同じレースだが日替りで content 変わる)
    金曜: メイン固定
    """
    all_races = get_weekend_graded_races(conn)
    if not all_races:
        return None

    graded = [r for r in all_races
              if detect_grade(r['race_name'], r.get('grade')) in ('G1', 'G2', 'G3')]
    if not graded:
        graded = all_races[:1]

    dow = now_jst().weekday()  # 0=月 ... 4=金

    if len(graded) == 1:
        return graded[0]

    if dow == 4:  # 金曜はメイン固定(まとめツイート用)
        return graded[0]

    # ローテ式: dow * 3 + slot を使うことで slot 跨ぎ・日跨ぎでも循環する
    idx = (dow * 3 + slot) % len(graded)
    return graded[idx]


def get_top_races(conn, n=3):
    """今週末のG1>G2>G3順で最大n件のレースを返す"""
    races = get_weekend_graded_races(conn)
    return races[:n]


def get_frame_stats(conn, venue, surface, distance):
    """枠順別複勝率を返す"""
    stats = []
    for pos in range(1, 9):
        r = conn.execute("""
            SELECT COUNT(*) as t, SUM(CASE WHEN r.finish_position<=3 THEN 1 ELSE 0 END) as t3
            FROM results r JOIN races ra ON r.race_id=ra.race_id
            WHERE ra.venue=? AND ra.surface=? AND ra.distance=? AND r.finish_position>0 AND r.post_position=?
        """, (venue, surface, distance, pos)).fetchone()
        rate = round(r['t3'] / r['t'] * 100, 1) if r['t'] > 0 else 0
        stats.append({'pos': pos, 'total': r['t'], 't3': r['t3'], 'rate': rate})
    return stats


def get_sire_course_stats(conn, sire, venue, surface, distance):
    """種牡馬×コースの成績"""
    r = conn.execute("""
        SELECT COUNT(*) as runs, SUM(CASE WHEN r.finish_position<=3 THEN 1 ELSE 0 END) as t3,
               SUM(CASE WHEN r.finish_position=1 THEN 1 ELSE 0 END) as wins, AVG(r.last_3f) as avg3f
        FROM results r JOIN races ra ON r.race_id=ra.race_id JOIN horses h ON r.horse_id=h.horse_id
        WHERE ra.venue=? AND ra.surface=? AND ra.distance=? AND r.finish_position>0 AND h.sire=?
    """, (venue, surface, distance, sire)).fetchone()
    if r and r['runs'] and r['runs'] > 0:
        return {'runs': r['runs'], 't3': r['t3'], 'wins': r['wins'],
                'rate': round(r['t3'] / r['runs'] * 100, 1),
                'avg3f': round(r['avg3f'], 1) if r['avg3f'] else None}
    return {'runs': 0, 't3': 0, 'wins': 0, 'rate': 0, 'avg3f': None}


def get_damsire_course_stats(conn, damsire, venue, surface, distance):
    """母父×コースの成績"""
    r = conn.execute("""
        SELECT COUNT(*) as runs, SUM(CASE WHEN r.finish_position<=3 THEN 1 ELSE 0 END) as t3
        FROM results r JOIN races ra ON r.race_id=ra.race_id JOIN horses h ON r.horse_id=h.horse_id
        WHERE ra.venue=? AND ra.surface=? AND ra.distance=? AND r.finish_position>0 AND h.damsire=?
    """, (venue, surface, distance, damsire)).fetchone()
    if r and r['runs'] and r['runs'] > 0:
        return {'runs': r['runs'], 't3': r['t3'], 'rate': round(r['t3'] / r['runs'] * 100, 1)}
    return {'runs': 0, 't3': 0, 'rate': 0}


def get_last3f_stats(conn, venue, surface, distance):
    """上がり3F別成績"""
    stats = []
    for label, lo, hi in [("33秒台以下", 30, 33.999), ("34秒台", 34, 34.999),
                           ("35秒台", 35, 35.999), ("36秒以上", 36, 45)]:
        r = conn.execute("""
            SELECT COUNT(*) as t, SUM(CASE WHEN r.finish_position<=3 THEN 1 ELSE 0 END) as t3,
                   SUM(CASE WHEN r.finish_position=1 THEN 1 ELSE 0 END) as w
            FROM results r JOIN races ra ON r.race_id=ra.race_id
            WHERE ra.venue=? AND ra.surface=? AND ra.distance=? AND r.finish_position>0
            AND r.last_3f BETWEEN ? AND ?
        """, (venue, surface, distance, lo, hi)).fetchone()
        if r and r['t'] > 0:
            stats.append({'label': label, 'total': r['t'],
                          'win_rate': round(r['w'] / r['t'] * 100, 1),
                          'top3_rate': round(r['t3'] / r['t'] * 100, 1)})
    return stats


def get_popularity_stats(conn, venue, surface, distance):
    """人気別成績"""
    stats = []
    for label, lo, hi in [("1-3番人気", 1, 3), ("4-6番人気", 4, 6),
                           ("7-9番人気", 7, 9), ("10番人気〜", 10, 30)]:
        r = conn.execute("""
            SELECT COUNT(*) as t, SUM(CASE WHEN r.finish_position<=3 THEN 1 ELSE 0 END) as t3,
                   SUM(CASE WHEN r.finish_position=1 THEN 1 ELSE 0 END) as w
            FROM results r JOIN races ra ON r.race_id=ra.race_id
            WHERE ra.venue=? AND ra.surface=? AND ra.distance=? AND r.finish_position>0
            AND r.popularity BETWEEN ? AND ?
        """, (venue, surface, distance, lo, hi)).fetchone()
        if r and r['t'] > 0:
            stats.append({'label': label, 'total': r['t'],
                          'win_rate': round(r['w'] / r['t'] * 100, 1),
                          'top3_rate': round(r['t3'] / r['t'] * 100, 1)})
    return stats


def entries_have_sire(entries):
    """出走馬リストにsire情報があるか判定"""
    return any(e['sire'] for e in entries) if entries else False


def get_course_top_sires(conn, venue, surface, distance, limit=5):
    """コース全体の種牡馬複勝率ランキング"""
    return conn.execute("""
        SELECT h.sire, COUNT(*) as runs,
               SUM(CASE WHEN r.finish_position<=3 THEN 1 ELSE 0 END) as t3,
               ROUND(SUM(CASE WHEN r.finish_position<=3 THEN 1.0 ELSE 0 END)/COUNT(*)*100, 1) as rate
        FROM results r JOIN races ra ON r.race_id=ra.race_id
        JOIN horses h ON r.horse_id=h.horse_id
        WHERE ra.venue=? AND ra.surface=? AND ra.distance=?
        AND r.finish_position>0 AND h.sire IS NOT NULL AND h.sire != ''
        GROUP BY h.sire HAVING runs >= 5
        ORDER BY rate DESC LIMIT ?
    """, (venue, surface, distance, limit)).fetchall()


def get_course_top_damsires(conn, venue, surface, distance, limit=5):
    """コース全体の母父複勝率ランキング"""
    return conn.execute("""
        SELECT h.damsire, COUNT(*) as runs,
               SUM(CASE WHEN r.finish_position<=3 THEN 1 ELSE 0 END) as t3,
               ROUND(SUM(CASE WHEN r.finish_position<=3 THEN 1.0 ELSE 0 END)/COUNT(*)*100, 1) as rate
        FROM results r JOIN races ra ON r.race_id=ra.race_id
        JOIN horses h ON r.horse_id=h.horse_id
        WHERE ra.venue=? AND ra.surface=? AND ra.distance=?
        AND r.finish_position>0 AND h.damsire IS NOT NULL AND h.damsire != ''
        GROUP BY h.damsire HAVING runs >= 5
        ORDER BY rate DESC LIMIT ?
    """, (venue, surface, distance, limit)).fetchall()


def get_race_entries(conn, race_id):
    """レースの出走馬情報を取得"""
    return conn.execute("""
        SELECT r.horse_number, r.horse_id, r.odds, r.popularity, r.post_position,
               h.horse_name, h.sire, h.damsire, j.jockey_name
        FROM results r LEFT JOIN horses h ON r.horse_id=h.horse_id
        LEFT JOIN jockeys j ON r.jockey_id=j.jockey_id
        WHERE r.race_id=? ORDER BY r.horse_number
    """, (race_id,)).fetchall()


def make_race_hashtags(race):
    """レースからハッシュタグを生成 (brand line は冗長なので省略)"""
    tag = race['race_name'].replace(' ', '').replace('　', '')
    return f"#{tag} #競馬予想 #AI予想"


# ─── DB統計情報（投稿に期間情報を自動挿入） ───

_db_stats_cache = None

def get_db_stats(conn=None):
    """DB全体の統計情報を取得（キャッシュ付き）"""
    global _db_stats_cache
    if _db_stats_cache:
        return _db_stats_cache

    def _query(c):
        row = c.execute("""
            SELECT MIN(race_date) as start_date, MAX(race_date) as end_date,
                   COUNT(DISTINCT race_id) as race_count
            FROM races WHERE race_date < date('now', '+1 day')
        """).fetchone()
        total = c.execute("""
            SELECT COUNT(*) as c FROM results WHERE finish_position > 0
        """).fetchone()
        return {
            'start_date': row['start_date'],
            'end_date': row['end_date'],
            'race_count': row['race_count'],
            'horse_count': total['c'],
        }

    if conn:
        _db_stats_cache = _query(conn)
    else:
        with get_db() as c:
            _db_stats_cache = _query(c)
    return _db_stats_cache


def data_credit(conn=None, short=False):
    """投稿末尾のクレジット（固定テキスト）"""
    if short:
        return "🧠 AI KEIBA PREDICTOR"
    else:
        return "🧠 AI KEIBA PREDICTOR | EQUINOX Lab"


def generate_analysis_column():
    """水曜: 分析コラム（3ツイート）"""
    columns = [
        {
            "title": "重馬場になると穴馬が走る理由",
            "t1_body": "雨で馬場が悪化すると\n人気馬が凡走することがあります\n\nなぜでしょう？",
            "t2_body": "📊 重馬場のポイント\n\n・パワーが必要になり小柄な馬は不利\n・泥を被ると嫌がる馬がいる\n・重馬場巧者の血統がある\n\n良馬場の実績だけでは判断できない",
            "t3_body": "💡 AIの対応\n\n当モデルは馬場状態ごとの\n過去成績を個別に分析\n\n「良馬場では凡走→重で好走」\nこういう馬をデータから発見します🧠",
        },
        {
            "title": "なぜ人気馬を買わない方がいいのか",
            "t1_body": "1番人気の勝率は約30%\nつまり70%は1番人気以外が勝つ\n\nでも多くの人は人気馬ばかり買う",
            "t2_body": "📊 回収率の現実\n\n1番人気の平均回収率は約80%\n→ 長期的には20%損する\n\n一方、穴馬の中には\n期待値が100%を超える馬もいる",
            "t3_body": "💡 AIが見るのは「期待値」\n\n期待値 = 勝率 × オッズ\n\nAIは勝率とオッズを比較し\n市場が過小評価している馬を発見\n\nこれが回収率187%の理由です📊",
        },
        {
            "title": "騎手と調教師の相性が成績を左右する",
            "t1_body": "同じ騎手でも\n調教師によって成績が全く違う\n\nこの「コンビ力」知ってますか？",
            "t2_body": "📊 コンビの重要性\n\n・調教方針と騎乗スタイルの相性\n・コミュニケーションの質\n・レース前の作戦共有\n\n実はAIの最重要特徴量がこれ",
            "t3_body": "💡 データで見ると\n\n当モデルの予測で最も影響力が大きい\nのが騎手×調教師コンビの複勝率\n\n人が見落としがちな相性を\nAIは数万レースから検出します🤝",
        },
        {
            "title": "3つのAIモデルを組み合わせる理由",
            "t1_body": "当予測は1つではなく\n3つのAIモデルを組み合わせています\n\nなぜ1つではダメなのか？",
            "t2_body": "📊 3モデル統合の仕組み\n\n1. LambdaRank→着順を直接予測\n2. 勝率モデル→1着の確率\n3. 複勝率モデル→3着以内の確率\n\n得意分野が異なる=弱点を補い合う",
            "t3_body": "💡 なぜ精度が上がる？\n\n例えばモデル1が◎でもモデル2,3が\n低評価なら危険信号\n\n3つが一致して高評価→信頼度が高い\nこの仕組みで安定した予測を実現🎯",
        },
    ]

    col = random.choice(columns)

    t1 = f"🧠 {col['title']}\n\n"
    t1 += col["t1_body"]
    t1 += f"\n\n{data_credit(short=True)}\n#競馬予想 #AI予想 🧵↓"

    t2 = col["t2_body"]

    t3 = col["t3_body"]

    return [t1, t2, t3]


def generate_pickup_horse():
    """木曜: 今週末の注目馬（3つの切り口）"""
    today = now_jst()

    # 今週末のレース日を特定
    with get_db() as conn:
        weekend = conn.execute("""
            SELECT DISTINCT race_date FROM races
            WHERE race_date > date('now') AND race_date <= date('now', '+7 days')
            ORDER BY race_date LIMIT 2
        """).fetchall()

    if not weekend:
        return None  # データ不足時はスキップ

    weekend_dates = [w['race_date'] for w in weekend]
    weekend_str = "・".join([f"{datetime.strptime(d,'%Y-%m-%d').month}/{datetime.strptime(d,'%Y-%m-%d').day}" for d in weekend_dates])

    with get_db() as conn:
        # ① 条件替わり: 前走は不得意条件で凡走→今週は得意条件
        dist_change = conn.execute("""
            WITH weekend_entries AS (
                SELECT r.horse_id, ra.race_name, ra.distance, ra.surface
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE ra.race_date > date('now') AND ra.race_date <= date('now', '+7 days')
                AND r.finish_position = 0
            ),
            last_race AS (
                SELECT r.horse_id, ra.distance as last_dist, ra.surface as last_surf,
                       r.finish_position as last_pos,
                       ROW_NUMBER() OVER (PARTITION BY r.horse_id ORDER BY ra.race_date DESC) as rn
                FROM results r JOIN races ra ON r.race_id = ra.race_id
                WHERE r.finish_position > 0
            ),
            good_cond AS (
                SELECT r.horse_id, ra.distance, ra.surface,
                       COUNT(*) as runs,
                       SUM(CASE WHEN r.finish_position <= 3 THEN 1 ELSE 0 END) as top3
                FROM results r JOIN races ra ON r.race_id = ra.race_id
                WHERE r.finish_position > 0
                GROUP BY r.horse_id, ra.distance, ra.surface
                HAVING runs >= 2 AND top3 >= 1
            )
            SELECT h.horse_name, we.race_name,
                   lr.last_surf||lr.last_dist||'m' as last_cond, lr.last_pos,
                   we.surface||we.distance||'m' as this_cond,
                   gc.runs, gc.top3
            FROM weekend_entries we
            JOIN horses h ON we.horse_id = h.horse_id
            JOIN last_race lr ON we.horse_id = lr.horse_id AND lr.rn = 1
            JOIN good_cond gc ON we.horse_id = gc.horse_id
                AND gc.distance = we.distance AND gc.surface = we.surface
            WHERE (we.distance != lr.last_dist OR we.surface != lr.last_surf)
            AND lr.last_pos >= 4
            ORDER BY CAST(gc.top3 AS FLOAT)/gc.runs DESC
            LIMIT 3
        """).fetchall()

        # ③ ベストタイム上位なのに前走凡走
        si_adv = conn.execute("""
            WITH weekend_entries AS (
                SELECT r.horse_id, ra.race_id, ra.race_name, ra.distance, ra.surface
                FROM results r JOIN races ra ON r.race_id = ra.race_id
                WHERE ra.race_date > date('now') AND ra.race_date <= date('now', '+7 days')
                AND r.finish_position = 0
            ),
            best_time AS (
                SELECT r.horse_id, MIN(r.finish_time_seconds) as bt, ra.distance, ra.surface
                FROM results r JOIN races ra ON r.race_id = ra.race_id
                WHERE r.finish_position > 0 AND r.finish_time_seconds > 0
                GROUP BY r.horse_id, ra.distance, ra.surface
            ),
            ranked AS (
                SELECT we.race_id, we.race_name, we.horse_id, bt.bt,
                       RANK() OVER (PARTITION BY we.race_id ORDER BY bt.bt) as rnk,
                       COUNT(*) OVER (PARTITION BY we.race_id) as total
                FROM weekend_entries we
                JOIN best_time bt ON we.horse_id = bt.horse_id
                    AND bt.distance = we.distance AND bt.surface = we.surface
            ),
            last_race AS (
                SELECT r.horse_id, r.finish_position as last_pos,
                       ROW_NUMBER() OVER (PARTITION BY r.horse_id ORDER BY ra.race_date DESC) as rn
                FROM results r JOIN races ra ON r.race_id = ra.race_id
                WHERE r.finish_position > 0
            )
            SELECT h.horse_name, rk.race_name, rk.rnk, rk.total, lr.last_pos
            FROM ranked rk
            JOIN horses h ON rk.horse_id = h.horse_id
            JOIN last_race lr ON rk.horse_id = lr.horse_id AND lr.rn = 1
            WHERE rk.rnk <= 2 AND lr.last_pos >= 4 AND rk.total >= 3
            ORDER BY rk.race_name, rk.rnk
            LIMIT 3
        """).fetchall()

        # ④ コース替わり: 得意競馬場に替わる
        venue_change = conn.execute("""
            WITH weekend_entries AS (
                SELECT r.horse_id, ra.race_name, ra.venue
                FROM results r JOIN races ra ON r.race_id = ra.race_id
                WHERE ra.race_date > date('now') AND ra.race_date <= date('now', '+7 days')
                AND r.finish_position = 0
            ),
            last_race AS (
                SELECT r.horse_id, ra.venue as last_venue, r.finish_position as last_pos,
                       ROW_NUMBER() OVER (PARTITION BY r.horse_id ORDER BY ra.race_date DESC) as rn
                FROM results r JOIN races ra ON r.race_id = ra.race_id
                WHERE r.finish_position > 0
            ),
            venue_rec AS (
                SELECT r.horse_id, ra.venue, COUNT(*) as runs,
                       SUM(CASE WHEN r.finish_position <= 3 THEN 1 ELSE 0 END) as top3
                FROM results r JOIN races ra ON r.race_id = ra.race_id
                WHERE r.finish_position > 0
                GROUP BY r.horse_id, ra.venue
                HAVING runs >= 2 AND top3 >= 1
            )
            SELECT h.horse_name, we.race_name, we.venue,
                   lr.last_venue, lr.last_pos, vr.runs, vr.top3
            FROM weekend_entries we
            JOIN horses h ON we.horse_id = h.horse_id
            JOIN last_race lr ON we.horse_id = lr.horse_id AND lr.rn = 1
            JOIN venue_rec vr ON we.horse_id = vr.horse_id AND vr.venue = we.venue
            WHERE we.venue != lr.last_venue AND lr.last_pos >= 4
            ORDER BY CAST(vr.top3 AS FLOAT)/vr.runs DESC
            LIMIT 3
        """).fetchall()

    # データがなければコラムに切替
    if not dist_change and not si_adv and not venue_change:
        return None  # データ不足時はスキップ

    # ── ツイート1: フック ──
    t1 = f"🔍 今週末({weekend_str})の注目馬\n\n"
    t1 += "前走凡走でも今回条件が変わる馬を\n"
    t1 += "3つの切り口でAIがピックアップ\n\n"
    t1 += f"{data_credit(short=True)}\n"
    t1 += "#競馬予想 #AI予想 🧵↓"

    # ── ツイート2: データ ──
    t2 = ""
    if dist_change:
        t2 += "🔄 条件替わりで好走期待\n"
        for h in dist_change:
            rate = round(h['top3']/h['runs']*100)
            t2 += f"・{h['horse_name']}({h['race_name']})\n"
            t2 += f" 前走{h['last_cond']}{h['last_pos']}着→今週{h['this_cond']}\n"
            t2 += f" 同条件{h['runs']}走 複勝率{rate}%\n"

    if venue_change:
        if t2:
            t2 += "\n"
        t2 += "🏟️ 得意競馬場に替わる\n"
        for h in venue_change:
            rate = round(h['top3']/h['runs']*100)
            t2 += f"・{h['horse_name']}({h['race_name']})\n"
            t2 += f" 前走{h['last_venue']}{h['last_pos']}着→今週{h['venue']}\n"
            t2 += f" {h['venue']}{h['runs']}走 複勝率{rate}%\n"

    if si_adv:
        if t2:
            t2 += "\n"
        t2 += "⏱️ タイム上位なのに前走凡走\n"
        for h in si_adv:
            t2 += f"・{h['horse_name']}({h['race_name']})\n"
            t2 += f" 前走{h['last_pos']}着→同距離ベスト{h['total']}頭中{h['rnk']}位\n"

    if not t2:
        t2 = "今週はデータ該当馬が少なめ。\n週末のAI予想をお待ちください。"

    # ── ツイート3: 解説 ──
    t3 = "💡 なぜ前走凡走馬に注目？\n\n"
    t3 += "前走の着順が悪いと人気が落ちる\n"
    t3 += "→ オッズが高くなる\n"
    t3 += "→ 条件が合えば期待値が跳ねる\n\n"
    t3 += "土曜朝にメインレースの\n"
    t3 += "AI予想を配信予定🔔"

    return [t1, t2, t3]


def generate_race_data_analysis():
    """火曜: 今週末の開催コース傾向をデータ分析"""
    today = now_jst()

    try:
        with get_db() as conn:
            # 今週末のレースの開催場所を取得
            venues = conn.execute("""
                SELECT DISTINCT venue FROM races
                WHERE race_date > date('now') AND race_date <= date('now', '+7 days')
                AND venue IS NOT NULL AND venue != ''
            """).fetchall()

            if not venues:
                return None  # データ不足時はスキップ

            venue_names = [v['venue'] for v in venues]
            tweets = []

            t1 = f"📊 今週末の開催場データ分析\n\n"
            t1 += f"🏟️ 開催: {'・'.join(venue_names)}\n\n"

            for vname in venue_names[:2]:  # 最大2場
                # そのコースの過去データ
                stats = conn.execute("""
                    SELECT COUNT(*) as total,
                           SUM(CASE WHEN r.popularity = 1 AND r.finish_position = 1 THEN 1 ELSE 0 END) as fav_wins,
                           SUM(CASE WHEN r.popularity >= 6 AND r.finish_position <= 3 THEN 1 ELSE 0 END) as dark_top3
                    FROM results r JOIN races ra ON r.race_id = ra.race_id
                    WHERE ra.venue = ? AND r.finish_position > 0
                """, (vname,)).fetchone()

                if stats and stats['total'] > 0:
                    fav_r = round(stats['fav_wins'] / stats['total'] * 100, 1)
                    dark_r = round(stats['dark_top3'] / stats['total'] * 100, 1)
                    t1 += f"📍 {vname}\n"
                    t1 += f" 1番人気勝率: {fav_r}%\n"
                    t1 += f" 6番人気以下の複勝率: {dark_r}%\n"
                    if fav_r >= 35:
                        t1 += f" → 堅い傾向。本命から入りたい🔒\n"
                    elif dark_r >= 20:
                        t1 += f" → 穴馬が走る。ワイド注目🎯\n"
                    else:
                        t1 += f" → 中穴狙いが面白い💡\n"
                    t1 += "\n"

            t1 += f"週末の予想はこちら👇\n"
            t1 += f"{NOTE_URL}\n\n"
            t1 += "#競馬データ #コース傾向"

            # ハッシュタグに場名追加
            for vname in venue_names[:2]:
                t1 += f" #{vname}競馬"

            return t1

    except Exception as e:
        print(f"⚠️ レースデータ分析エラー: {e}")
        return None  # データ不足時はスキップ


def generate_horse_spotlight():
    """水曜: 今週末出走予定の注目馬をスポットライト"""
    today = now_jst()

    try:
        with get_db() as conn:
            # 今週末のメインレース(11R)の予測データから上位馬を取得
            main_races = conn.execute("""
                SELECT ra.race_id, ra.race_name, ra.venue, ra.surface,
                       ra.distance, ra.grade, ra.race_date,
                       pc.predictions_json
                FROM races ra
                JOIN predictions_cache pc ON ra.race_id = pc.race_id
                WHERE ra.race_date > date('now') AND ra.race_date <= date('now', '+7 days')
                AND ra.race_number >= 10
                ORDER BY ra.grade DESC, ra.race_number DESC
            """).fetchall()

            if not main_races:
                return None  # データ不足時はスキップ

            # 最もグレードの高いレースから注目馬を選出
            spotlight_race = main_races[0]
            preds = json.loads(spotlight_race['predictions_json']) if spotlight_race['predictions_json'] else []

            if len(preds) < 2:
                return None  # データ不足時はスキップ

            top = preds[0]
            rival = preds[1]

            rd = datetime.strptime(spotlight_race['race_date'], '%Y-%m-%d')
            dow = ['月','火','水','木','金','土','日'][rd.weekday()]

            t = f"🌟 今週の注目馬\n\n"
            t += f"📍 {spotlight_race['venue']} {spotlight_race['race_name']}\n"
            if spotlight_race.get('grade'):
                t += f"🏆 {spotlight_race['grade']} "
            t += f"{spotlight_race['surface']}{spotlight_race['distance']}m\n"
            t += f"📅 {rd.month}/{rd.day}({dow})\n\n"

            # ◎の馬の詳細
            win_pct = top.get('pred_win_pct', top.get('pred_win', 0))
            t += f"◎ {top.get('horse_name', '?')}\n"
            t += f" AI勝率: {win_pct:.1f}%\n"
            if top.get('reasons'):
                for r in top['reasons'][:2]:
                    t += f" ✅ {r}\n"

            t += f"\n○ {rival.get('horse_name', '?')}\n"
            rival_pct = rival.get('pred_win_pct', rival.get('pred_win', 0))
            t += f" AI勝率: {rival_pct:.1f}%\n\n"

            gap = win_pct - rival_pct
            if gap >= 10:
                t += "→ ◎が頭一つ抜けている構図🔥\n"
            elif gap >= 3:
                t += "→ 実力拮抗。展開次第で逆転も💥\n"
            else:
                t += "→ 大混戦。穴馬の台頭にも注意⚡\n"

            t += f"\n詳細予想は週末朝に配信👇\n"
            t += f"{NOTE_URL}\n\n"

            # ハッシュタグ
            race_tag = spotlight_race['race_name'].replace(' ', '').replace('　', '')
            t += f"#競馬予想 #{race_tag}"

            return t

    except Exception as e:
        print(f"⚠️ 注目馬スポットライトエラー: {e}")
        return None  # データ不足時はスキップ


def generate_weekend_preview():
    """金曜: 今週末の重賞＋AI注目馬（3ツイート）"""
    today = now_jst()

    with get_db() as conn:
        # 今週末のレース（11R）を取得
        races_11r = conn.execute("""
            SELECT ra.race_id, ra.race_name, ra.venue, ra.distance,
                   ra.surface, ra.race_date
            FROM races ra
            WHERE ra.race_date > date('now') AND ra.race_date <= date('now', '+7 days')
            AND ra.race_number = 11
            ORDER BY ra.race_date, ra.venue
        """).fetchall()

        if not races_11r:
            return None  # データ不足時はスキップ

        # 各レースのAI予測上位を取得
        race_picks = []
        for race in races_11r:
            pred = conn.execute("""
                SELECT predictions_json FROM predictions_cache
                WHERE race_id = ?
            """, (race['race_id'],)).fetchone()

            top_horses = []
            if pred and pred['predictions_json']:
                preds = json.loads(pred['predictions_json'])
                top_horses = [p.get('horse_name', '?') for p in preds[:2]]

            race_picks.append({
                'venue': race['venue'],
                'name': race['race_name'],
                'surface': race['surface'],
                'distance': race['distance'],
                'date': race['race_date'],
                'picks': top_horses,
            })

        # 開催場所
        venues = list(set(r['venue'] for r in races_11r))
        dates = sorted(set(r['race_date'] for r in races_11r))
        dow_labels = {5: '土', 6: '日', 0: '月'}
        date_parts = []
        for d in dates:
            dt = datetime.strptime(d, '%Y-%m-%d')
            dow = dow_labels.get(dt.weekday(), '')
            date_parts.append(f"{dt.month}/{dt.day}({dow})")
        date_str = "・".join(date_parts)

    # ツイート1: フック
    t1 = f"📅 今週末のレース\n"
    t1 += f"{date_str}\n"
    t1 += f"開催: {'・'.join(venues)}\n\n"
    t1 += "メインレースのAI注目馬を\n"
    t1 += "一足先にチラ見せ\n\n"
    t1 += f"{data_credit(short=True)}\n"
    t1 += "#競馬予想 #AI予想 🧵↓"

    # ツイート2: 日付別にグループ化
    t2 = ""
    from itertools import groupby
    for date_key, group in groupby(race_picks, key=lambda x: x['date']):
        dt = datetime.strptime(date_key, '%Y-%m-%d')
        dow = dow_labels.get(dt.weekday(), '')
        t2 += f"📆 {dt.month}/{dt.day}({dow})\n"
        for rp in group:
            t2 += f"🏇{rp['venue']} {rp['name']}\n"
            t2 += f" {rp['surface']}{rp['distance']}m"
            if rp['picks']:
                t2 += f" AI注目:{'/'.join(rp['picks'])}\n"
            else:
                t2 += " AI注目:分析中\n"
        t2 += "\n"

    # ツイート3: 配信案内
    t3 = "🔔 明日朝8時に詳細予想を配信\n\n"
    t3 += "各レースの◎○▲△と\n"
    t3 += "買い目まで公開します\n\n"
    t3 += "フォロー&通知ONで\n"
    t3 += "見逃さないようにしてください👀"

    return [t1, t2, t3]


# ─── 土曜夜: 全レース答え合わせ ───
def cmd_answer_check(args):
    """当日の全レースの◎の着順を集計して答え合わせ投稿"""
    date_str = args.date
    dt = datetime.strptime(date_str, "%Y%m%d")
    weekday = ["月", "火", "水", "木", "金", "土", "日"][dt.weekday()]
    date_label = f"{dt.month}/{dt.day}({weekday})"
    date_hyphen = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    with get_db() as conn:
        # 全レース（11Rだけでなく全R）の◎の着順を取得
        races = conn.execute("""
            SELECT ra.race_id, ra.race_name, ra.venue, ra.race_number,
                   pc.predictions_json
            FROM races ra
            JOIN predictions_cache pc ON ra.race_id = pc.race_id
            WHERE (ra.race_date = ? OR ra.race_date = ?)
            ORDER BY ra.venue, ra.race_number
        """, (date_str, date_hyphen)).fetchall()

        if not races:
            print(f"❌ {date_str} の予測データがありません")
            return

        total = 0
        honmei_wins = 0
        honmei_top3 = 0
        highlights = []  # 高オッズ的中

        for race in races:
            preds = json.loads(race['predictions_json']) if race['predictions_json'] else []
            if not preds:
                continue

            # ◎を特定
            honmei = None
            for p in preds:
                if p.get('mark') == '◎':
                    honmei = p
                    break
            if not honmei:
                honmei = preds[0] if preds else None
            if not honmei:
                continue

            hn = honmei.get('horse_number', 0)
            horse_name = honmei.get('horse_name', '?')

            # 着順取得
            actual = conn.execute("""
                SELECT finish_position, odds FROM results
                WHERE race_id = ? AND horse_number = ?
                AND finish_position > 0
            """, (race['race_id'], hn)).fetchone()

            if not actual:
                continue

            total += 1
            fp = actual['finish_position']
            odds = actual['odds'] or 0

            if fp == 1:
                honmei_wins += 1
                honmei_top3 += 1
                if odds >= 3.0:  # 3倍以上の的中はハイライト
                    highlights.append({
                        'venue': race['venue'],
                        'race_number': race['race_number'],
                        'horse_name': horse_name,
                        'odds': odds,
                        'position': fp,
                    })
            elif fp <= 3:
                honmei_top3 += 1

    if total == 0:
        print(f"❌ {date_str} の結果がまだ出ていません")
        return

    win_rate = round(honmei_wins / total * 100) if total > 0 else 0
    top3_rate = round(honmei_top3 / total * 100) if total > 0 else 0

    # ── ツイート1: 概要 ──
    t1 = f"📊 {date_label} AI予想の答え合わせ\n\n"
    t1 += f"全{total}レースの◎結果:\n"
    t1 += f"🥇 1着: {honmei_wins}回（{win_rate}%）\n"
    t1 += f"🏅 3着内: {honmei_top3}回（{top3_rate}%）\n\n"

    if win_rate >= 30:
        t1 += "好調をキープ📈\n"
    elif win_rate >= 20:
        t1 += "安定の成績📊\n"
    else:
        t1 += "反省点を次に活かします📝\n"

    t1 += f"\n{data_credit(short=True)}\n#競馬予想 #AI予想 #競馬結果"

    # ── ツイート2: ハイライト ──
    t2 = ""
    if highlights:
        t2 = "💰 注目の的中\n\n"
        for h in sorted(highlights, key=lambda x: x['odds'], reverse=True)[:5]:
            t2 += f"✅ {h['venue']}{h['race_number']}R "
            t2 += f"◎{h['horse_name']} → {h['position']}着"
            t2 += f"（{h['odds']:.1f}倍）\n"
    else:
        t2 = "📋 1着的中は人気馬中心でした\n\n"
        t2 += "高配当の的中はなし\n"
        t2 += "次回は穴馬の精度向上を目指します"

    # ── ツイート3: 次回予告 ──
    is_saturday = dt.weekday() == 5
    t3 = ""
    if is_saturday:
        t3 = "🔔 明日もAI予想を配信\n\n"
        t3 += "朝7時に全レース予想を投稿\n"
        t3 += "フォロー&通知ONで見逃さない👀"
    else:
        t3 = "🔔 来週も毎日配信\n\n"
        t3 += "火〜木: 重賞データ分析（note）\n"
        t3 += "土日朝: 全レースAI予想\n\n"
        t3 += "フォローして見逃さないでください🔔"

    tweets = [t1, t2, t3]

    client = None
    threads_client = load_threads_client()
    if not args.dry_run:
        client = load_x_client()
        if not client:
            return

    post_thread(client, tweets, dry_run=args.dry_run, threads_client=threads_client)


# ─── 日曜夜: 週間ROIレビュー ───
def cmd_weekly_review(args):
    """今週末（土日）の全レースROIをまとめて報告"""
    today = now_jst()
    # 直近の土日を取得
    if today.weekday() == 6:  # 日曜
        sun = today
        sat = today - timedelta(days=1)
    elif today.weekday() == 5:  # 土曜
        sat = today
        sun = today + timedelta(days=1)
    else:
        # 平日の場合は先週末
        days_since_sun = today.weekday() + 1
        sun = today - timedelta(days=days_since_sun)
        sat = sun - timedelta(days=1)

    sat_str = sat.strftime("%Y-%m-%d")
    sun_str = sun.strftime("%Y-%m-%d")
    dr = f"{sat.month}/{sat.day}-{sun.month}/{sun.day}"

    with get_db() as conn:
        # 全レースの◎の着順を集計
        races = conn.execute("""
            SELECT ra.race_id, ra.race_name, ra.venue,
                   pc.predictions_json, pc.all_bets_json
            FROM races ra
            JOIN predictions_cache pc ON ra.race_id = pc.race_id
            WHERE ra.race_date IN (?, ?)
            ORDER BY ra.race_date, ra.venue, ra.race_number
        """, (sat_str, sun_str)).fetchall()

        if not races:
            print(f"❌ {dr} の予測データがありません")
            # フォールバック
            args_mock = type('Args', (), {'dry_run': getattr(args, 'dry_run', False)})()
            return cmd_weekday(args_mock)

        total_races = 0
        honmei_wins = 0
        honmei_top3 = 0
        total_invested = 0
        total_payout = 0
        bet_type_stats = {}  # 券種別の投資/回収

        for race in races:
            preds = json.loads(race['predictions_json']) if race['predictions_json'] else []
            all_bets = json.loads(race['all_bets_json']) if race['all_bets_json'] else {}

            if not preds:
                continue

            # ◎を特定
            honmei = None
            for p in preds:
                if p.get('mark') == '◎':
                    honmei = p
                    break
            if not honmei and preds:
                honmei = preds[0]

            if honmei:
                hn = honmei.get('horse_number', 0)
                actual = conn.execute("""
                    SELECT finish_position FROM results
                    WHERE race_id = ? AND horse_number = ?
                    AND finish_position > 0
                """, (race['race_id'], hn)).fetchone()

                if actual:
                    total_races += 1
                    if actual['finish_position'] == 1:
                        honmei_wins += 1
                        honmei_top3 += 1
                    elif actual['finish_position'] <= 3:
                        honmei_top3 += 1

            # 券種別ROI計算
            payouts = conn.execute("""
                SELECT bet_type, combination, payout_amount
                FROM payouts WHERE race_id = ?
            """, (race['race_id'],)).fetchall()
            payout_map = {(p['bet_type'], p['combination']): p['payout_amount'] for p in payouts}

            finishes = conn.execute("""
                SELECT horse_number, finish_position FROM results
                WHERE race_id = ? AND finish_position > 0
            """, (race['race_id'],)).fetchall()
            finish_map = {f['horse_number']: f['finish_position'] for f in finishes}

            for bt, bt_bets in all_bets.items():
                if bt not in bet_type_stats:
                    bet_type_stats[bt] = {'invested': 0, 'payout': 0}

                for b in bt_bets:
                    amount = b.get('amount', 100)
                    bet_type_stats[bt]['invested'] += amount
                    total_invested += amount

                    hns = b.get('horse_numbers', [])
                    is_hit = False
                    actual_payout = 0

                    if bt == '単勝' and len(hns) >= 1:
                        if finish_map.get(hns[0]) == 1:
                            is_hit = True
                            actual_payout = payout_map.get(('単勝', str(hns[0])), 0)
                    elif bt == '三連単' and len(hns) >= 3:
                        ordered = [h for h, f in sorted(finish_map.items(), key=lambda x: x[1]) if f <= 3]
                        if hns[:3] == ordered[:3]:
                            is_hit = True
                            combo = '-'.join(str(h) for h in hns[:3])
                            actual_payout = payout_map.get(('三連単', combo), 0)

                    if is_hit and actual_payout > 0:
                        payout_val = int(actual_payout * (amount / 100))
                        bet_type_stats[bt]['payout'] += payout_val
                        total_payout += payout_val

    if total_races == 0:
        print(f"❌ {dr} の結果がまだ出ていません")
        return

    win_rate = round(honmei_wins / total_races * 100) if total_races > 0 else 0
    top3_rate = round(honmei_top3 / total_races * 100) if total_races > 0 else 0
    overall_roi = round(total_payout / total_invested * 100) if total_invested > 0 else 0

    # ── ツイート1: 週間レポート ──
    t1 = f"📊 今週のAI予想 成績レポート\n"
    t1 += f"({dr})\n\n"
    t1 += f"対象: 全{total_races}レース\n"
    t1 += f"◎1着率: {win_rate}%（{honmei_wins}/{total_races}R）\n"
    t1 += f"◎複勝率: {top3_rate}%（{honmei_top3}/{total_races}R）\n\n"

    # 券種別ROI
    for bt in ['単勝', '三連単']:
        if bt in bet_type_stats and bet_type_stats[bt]['invested'] > 0:
            roi = round(bet_type_stats[bt]['payout'] / bet_type_stats[bt]['invested'] * 100)
            emoji = "✅" if roi >= 100 else "📉"
            t1 += f"{bt}ROI: {roi}%{emoji}\n"

    t1 += f"\n{data_credit(short=True)}\n#競馬予想 #AI予想 #競馬結果"

    # ── ツイート2: 透明性 ──
    profit = int(total_payout - total_invested)
    t2 = "📋 収支の透明レポート\n\n"
    t2 += f"投資: {int(total_invested):,}円\n"
    t2 += f"回収: {int(total_payout):,}円\n"
    t2 += f"収支: {'+' if profit >= 0 else ''}{profit:,}円\n"
    t2 += f"ROI: {overall_roi}%\n\n"

    if overall_roi >= 100:
        t2 += "プラス収支で週を終了📈"
    else:
        t2 += "マイナスですが長期で見てください📊"

    # ── ツイート3: 来週の予告 ──
    t3 = "🔔 来週のスケジュール\n\n"
    t3 += "火〜木 12:00: 重賞データ分析\n"
    t3 += "土日 7:00: 全レースAI予想\n"
    t3 += "土日 20:00: 結果報告\n\n"
    t3 += "フォローして見逃さないでください🔔\n"
    t3 += "noteで重賞の無料分析も公開中👇"

    tweets = [t1, t2, t3]

    client = None
    threads_client = load_threads_client()
    if not args.dry_run:
        client = load_x_client()
        if not client:
            return

    post_thread(client, tweets, dry_run=args.dry_run, threads_client=threads_client)


# ─── 平日: note記事プロモ ───
def generate_note_promo():
    """articlesフォルダから「今週末のレース」記事のみ選んでプロモツイートを生成"""
    import glob
    import re

    articles_dir = os.path.join(os.path.dirname(__file__), "articles")
    today = now_jst()

    # 今週末の土日の日付を算出
    days_until_sat = (5 - today.weekday()) % 7
    if days_until_sat == 0 and today.weekday() == 5:
        days_until_sat = 0  # 土曜当日
    next_sat = today + timedelta(days=days_until_sat)
    next_sun = next_sat + timedelta(days=1)

    # DBから今週末のレース名を取得（フィルタ用）
    upcoming_race_names = set()
    try:
        with get_db() as conn:
            sat_str = next_sat.strftime("%Y-%m-%d")
            sun_str = next_sun.strftime("%Y-%m-%d")
            races = conn.execute("""
                SELECT DISTINCT race_name FROM races
                WHERE race_date IN (?, ?)
                AND (grade IS NOT NULL AND grade != '')
            """, (sat_str, sun_str)).fetchall()
            for r in races:
                upcoming_race_names.add(r['race_name'])
            print(f"📅 今週末のレース: {', '.join(upcoming_race_names) if upcoming_race_names else 'なし'}")
    except Exception as e:
        print(f"⚠️ DB参照エラー: {e}")

    # 記事を検索（review_やyyyymmdd形式は除外）
    promo_files = []
    if os.path.exists(articles_dir):
        for f in glob.glob(os.path.join(articles_dir, "*.md")):
            basename = os.path.basename(f)
            # review_、日付形式、_x.txt、part2（後編は金曜用）を除外
            if basename.startswith("review_") or basename.startswith("2026"):
                continue
            if "_x." in basename:
                continue

            # 記事のタイトルからレース名を抽出してフィルタ
            try:
                with open(f, 'r', encoding='utf-8') as fh:
                    first_lines = fh.read(500)

                # 記事タイトルにDBの今週末レース名が含まれているかチェック
                is_upcoming = False
                if upcoming_race_names:
                    for race_name in upcoming_race_names:
                        # レース名の部分一致（大阪杯 ↔ osaka_hai など）
                        race_short = race_name.replace('（', '').replace('）', '')
                        if race_short in first_lines:
                            is_upcoming = True
                            break

                # ファイル名からもチェック（osaka_hai, derby_ct など）
                race_name_map = {
                    '大阪杯': ['osaka_hai'],
                    'ダービー卿': ['derby_ct'],
                    'チャーチルダウンズ': ['churchill'],
                    '高松宮': ['takamatsunomiya'],
                    '毎日杯': ['mainichi_hai'],
                    '日経賞': ['nikkei_sho'],
                    'マーチS': ['march_s'],
                }
                if not is_upcoming:
                    for rname, patterns in race_name_map.items():
                        if any(p in basename for p in patterns):
                            if rname in str(upcoming_race_names) or any(rname in n for n in upcoming_race_names):
                                is_upcoming = True
                                break

                if is_upcoming:
                    promo_files.append(f)
                    print(f"  ✅ 今週末対象: {basename}")
                else:
                    print(f"  ⏭️ スキップ（過去レース）: {basename}")

            except Exception:
                continue

    if not promo_files:
        print("📝 今週末対象の記事なし → フォールバック")
        return None  # データ不足時はスキップ

    # 更新日順でソート
    promo_files.sort(key=os.path.getmtime, reverse=True)

    # 今日の曜日で記事を選ぶ（火=0番目, 水=1番目, 木=2番目）
    dow = today.weekday()
    idx = dow - 1  # 火=0, 水=1, 木=2
    if idx < 0 or idx >= len(promo_files):
        idx = 0

    article_path = promo_files[min(idx, len(promo_files) - 1)]
    print(f"📰 選択記事: {os.path.basename(article_path)}")

    # 記事の内容を読み取り
    with open(article_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # タイトルを抽出（最初の # 行）
    title = ""
    key_points = []
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("# ") and not title:
            title = line.replace("# ", "")
        # ポイントになるデータを抽出（太字や✅の行）
        if ("**" in line and "→" in line and len(key_points) < 3):
            key_points.append(line.replace("**", ""))
        elif (line.startswith("✅") or line.startswith("❌")) and len(key_points) < 3:
            key_points.append(line)

    if not title:
        return None  # データ不足時はスキップ

    # レース名からハッシュタグを動的生成
    race_hashtags = ""
    hashtag_map = {
        '大阪杯': '#大阪杯', 'ダービー卿': '#ダービー卿CT',
        'チャーチルダウンズ': '#チャーチルダウンズC', '高松宮': '#高松宮記念',
        '毎日杯': '#毎日杯', '日経賞': '#日経賞', 'マーチ': '#マーチS',
        '桜花賞': '#桜花賞', '皐月賞': '#皐月賞', '天皇賞': '#天皇賞',
        '宝塚記念': '#宝塚記念', 'NHKマイル': '#NHKマイルC',
    }
    for race_key, hashtag in hashtag_map.items():
        if race_key in title:
            race_hashtags = hashtag
            break

    # ── ツイート1: フック ──
    t1 = f"📊 {title}\n\n"
    t1 += "noteで無料公開中👇\n\n"
    if key_points:
        for kp in key_points[:2]:
            t1 += f"・{kp}\n"
        t1 += "\n"
    t1 += f"{race_hashtags} #競馬データ #競馬予想"

    # ── ツイート2: 記事からデータを引用 ──
    t2 = "📋 記事のポイント\n\n"
    if "多次元分析" in content or "AI" in title:
        t2 += "AIの多次元分析で\n"
        t2 += "従来の一次元分析では見えない\n"
        t2 += "複合パターンを解説しています\n\n"
    if key_points:
        for kp in key_points:
            t2 += f"📌 {kp}\n"
    else:
        t2 += "傾向データ＋注目馬＋危険な人気馬\n"
        t2 += "をデータで分析しています"

    # ── ツイート3: CTA ──
    t3 = "🔔 noteで全文を無料公開中\n\n"
    t3 += "プロフィールのリンクからどうぞ👀\n\n"

    # 今週末のメインレースを予告
    if upcoming_race_names:
        main_races = [n for n in upcoming_race_names if any(g in n for g in ['G', '杯', '記念', 'S', 'ステークス'])]
        if main_races:
            t3 += f"📢 今週末は {main_races[0]} 🔥\n"
            t3 += "AI予想は土日朝7時に配信します\n\n"

    t3 += "フォローして見逃さないでください！"

    return [t1, t2, t3]



# ─── ファクトチェック ───
def fact_check_tweet_or_thread(tweet_or_thread):
    """tweet (str) または thread (list[str]) を受け取り、全要素を fact_check する。

    5/26 の slot_composer 導入で `_build_weekday_post` が list を返すようになったが、
    cmd_morning/evening は string 前提で `fact_check_tweet(tweet)` を呼んでいた結果、
    5/27 朝の morning cron が TypeError で失敗。
    この helper で str/list 両方を統一的に処理する。
    """
    items = tweet_or_thread if isinstance(tweet_or_thread, list) else [tweet_or_thread]
    for t in items:
        if not isinstance(t, str):
            print(f"⚠️ fact_check: 予期しない型 {type(t).__name__} → スキップ")
            return False
        if not fact_check_tweet(t):
            return False
    return True


def fact_check_tweet(tweet_text):
    """ツイートの数値データをDBと照合して検証する。
    数値が含まれるツイートの場合、DBからデータを再取得して一致を確認。
    不一致があればWarningを出す。

    Returns:
        True: チェック通過（投稿OK）
        False: 致命的な問題あり（投稿ブロック推奨）
    """
    import re
    issues = []
    critical = False  # 致命的 → 投稿ブロック

    # 勝率/複勝率の表記を検証
    pct_matches = re.findall(r'(\d+\.?\d*)%', tweet_text)
    for pct in pct_matches:
        val = float(pct)
        if val > 100:
            issues.append(f"🚫 {val}% は100%を超えています")
            critical = True
        if val == 0:
            issues.append(f"⚠️ 0% は不自然な値です")

    # 金額表記の検証
    yen_matches = re.findall(r'([\d,]+)円', tweet_text)
    for yen in yen_matches:
        val = int(yen.replace(',', ''))
        if val > 10000000:  # 1000万円超
            issues.append(f"🚫 {yen}円 は異常に高額です")
            critical = True

    # ROIの検証
    roi_matches = re.findall(r'ROI[:\s]*(\d+)%', tweet_text)
    for roi in roi_matches:
        val = int(roi)
        if val > 10000:
            issues.append(f"🚫 ROI {val}% は異常値です")
            critical = True

    # ── 内容整合性チェック ──
    # 成績レポートで分析数が極端に少ない
    r_match = re.search(r'(\d+)R分析', tweet_text)
    if r_match:
        race_count = int(r_match.group(1))
        if race_count < 3 and '成績' in tweet_text:
            issues.append(f"🚫 成績レポートで{race_count}R分析は少なすぎます（通常6R以上）")
            critical = True

    # 全て0/0の無意味なレポート
    zero_matches = re.findall(r'0/\d+ \(0%\)', tweet_text)
    all_stats = re.findall(r'(\d+)/(\d+)', tweet_text)
    if all_stats and all(n == '0' for n, _ in all_stats):
        issues.append("🚫 全ての成績が0件 — データが正しく取得されていません")
        critical = True

    # 的中報告で投資0円
    if '的中' in tweet_text or '回収' in tweet_text:
        invest_match = re.search(r'投資[:\s]*([\d,]+)円', tweet_text)
        if invest_match and int(invest_match.group(1).replace(',', '')) == 0:
            issues.append("🚫 投資0円の的中報告は不正です")
            critical = True

    # ── v9 「中身なし」検出 — データ薄い slot は投稿ブロック ──
    # 「(○○データ収集中)」「該当なし」「(取得中)」等のプレースホルダー残存
    empty_patterns = [
        r'\(.*データ?(収集|取得)中\)',
        r'\(.*未確定\)',
        r'\(.*未登録\)',
        r'該当(する|なし|データなし)',
        r'\[URL\]',
        r'\(URL\)',
        r'\(記事公開後',
        r'\[TODO\]',
        r'\[馬名\]',
    ]
    for pat in empty_patterns:
        if re.search(pat, tweet_text):
            issues.append(f"🚫 中身なしプレースホルダー: {pat}")
            critical = True

    # 投稿に具体的な馬名/馬番が1つも含まれない 「中身なし」検出
    # → 「{番号}番 {馬名}」or「⭐{番号}番」or「{年}年:{馬名}」のパターン
    has_specific_horse = (
        re.search(r'\d{1,2}番\s*[ァ-ヴー]', tweet_text) or
        re.search(r'⭐\s*\d', tweet_text) or
        re.search(r'\d{4}\s+[ァ-ヴー]{3,}', tweet_text) or
        re.search(r'\d{4}年:?\s*[ァ-ヴー]{3,}', tweet_text)
    )
    # ただし馬名不要な投稿カテゴリは許容 (universal_fallback の8テーマ + その他)
    # v9 universal_fallback で出る theme_title を網羅
    no_horse_ok_keywords = [
        'ラインナップ', '配信お知らせ', '配信予定',
        # post_predict のフッター CTA (馬名なしが正常)
        'AI予想印です', '結果は夕方に速報', '事前公開しています',
        'レース前に事前公開',
        # v8 builder 由来
        '騎手コース適性', '騎手TOP', '父TOP', '母父TOP',
        # v9 universal_fallback の 8テーマ
        '枠順傾向', '脚質傾向', '末脚の威力', '人気別 成績',
        'コース×父血統', 'コース×母父', 'コース騎手TOP', '過去勝ち馬パターン',
        '独自パターン分析', '勝ち馬パターン',
        # v9.3 で使う theme_title (上の部分一致だけだと拾えないため追加)
        '父血統TOP', '人気別傾向',
        # 汎用カテゴリ
        'コース傾向', 'コース分析', '上がり3F最速馬の実績',
        '末脚分析', '末脚有望馬',
        # 見出し記号からの判定
        '【勝ち馬パターン', '当コース', '【枠順', '【脚質',
        '【人気別', '【末脚', '【コース',
    ]
    needs_horse = not any(kw in tweet_text for kw in no_horse_ok_keywords)
    if needs_horse and not has_specific_horse:
        issues.append("🚫 具体的な馬名/馬番が含まれない投稿(中身なし)")
        critical = True

    # 本文(ハッシュタグ・URLを除く)が極端に短い (有意なinfo がない)
    body_lines = [
        ln for ln in tweet_text.split('\n')
        if ln.strip() and not ln.startswith('#')
        and not re.match(r'^https?://', ln.strip())
    ]
    if len(body_lines) < 5:
        # 5行未満なら情報量不足の可能性
        issues.append(f"⚠️ 本文の行数が少ない({len(body_lines)}行)、内容希薄の可能性")

    # 既存 regex チェック結果
    if issues:
        print("🔍 ファクトチェック結果 (regex):")
        for issue in issues:
            print(f"  {issue}")
        if critical:
            print("  🚫 致命的な問題があるため投稿をブロックします")
            return False

    # 🆕 DB クロスファクトチェック (2026-05-25 追加)
    # 「過去6年」と書いて 2022/2023 がない、DB に存在しない馬名等を検出
    try:
        from tweet_fact_check import db_fact_check
        passed_db, db_issues = db_fact_check(tweet_text)
        if db_issues:
            print("🔍 ファクトチェック結果 (DB クロス):")
            for issue in db_issues:
                print(f"  {issue}")
        if not passed_db:
            print("  🚫 DB 整合性エラーで投稿ブロック")
            return False
    except ImportError:
        print("  ℹ️ tweet_fact_check 未導入 (skip)")
    except Exception as e:
        print(f"  ℹ️ DB ファクトチェック skip (エラー): {e}")

    if not issues:
        print("✅ ファクトチェック通過")
    return True


# ─── 的中速報ポスト ───
def cmd_hit_flash(args):
    """的中速報ツイート — cmd_results に委譲(印別着順、金額なし)。

    旧実装は「投資/回収/ROI」を含む金額入りの的中速報だったが、
    ユーザー要件に統一: 結果系の post は金額表示なし、印別着順のみ。
    cmd_hit_flash と cmd_results の出力フォーマットを単一化することで、
    cron schedule が hit_flash でも results でも同じ post が出るようになる。
    """
    print("ℹ️ hit_flash → results に委譲(金額なし・印別着順フォーマット)")
    return cmd_results(args)


def _legacy_cmd_hit_flash_DEPRECATED(args):  # noqa: N802
    """旧 hit_flash 実装(金額/ROI 入り)。互換目的で参照可能だが呼ばない。"""
    date_str = args.date
    dt = datetime.strptime(date_str, "%Y%m%d")
    weekday = ["月", "火", "水", "木", "金", "土", "日"][dt.weekday()]
    date_label = f"{dt.month}/{dt.day}({weekday})"
    date_hyphen = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    with get_db() as conn:
        # 全レース対象（11Rだけでなく）
        races = conn.execute("""
            SELECT ra.race_id, ra.race_name, ra.venue, ra.grade, ra.race_number,
                   pc.predictions_json, pc.all_bets_json, pc.confidence
            FROM races ra
            JOIN predictions_cache pc ON ra.race_id = pc.race_id
            WHERE (ra.race_date = ? OR ra.race_date = ?)
            ORDER BY ra.venue, ra.race_number
        """, (date_str, date_hyphen)).fetchall()

        if not races:
            print(f"❌ {date_str} の予測データがありません")
            return

        total_invested = 0
        total_payout = 0
        total_bets = 0
        total_hits = 0
        hit_details = []  # 的中レース詳細
        honmei_total = 0
        honmei_win = 0
        honmei_top3 = 0
        races_analyzed = 0

        for race in races:
            preds = json.loads(race['predictions_json']) if race['predictions_json'] else []
            all_bets = json.loads(race['all_bets_json']) if race['all_bets_json'] else {}

            # 着順取得
            finishes = conn.execute("""
                SELECT horse_number, finish_position, odds FROM results
                WHERE race_id = ? AND finish_position > 0
            """, (race['race_id'],)).fetchall()
            finish_map = {f['horse_number']: f['finish_position'] for f in finishes}

            if not finish_map:
                continue
            races_analyzed += 1

            # ◎成績
            honmei = next((p for p in preds if p.get('mark') == '◎'), None)
            if honmei:
                hn = honmei['horse_number']
                f = finish_map.get(hn, 99)
                honmei_total += 1
                if f == 1: honmei_win += 1
                if f <= 3: honmei_top3 += 1

            # 配当情報取得
            payouts = conn.execute("""
                SELECT bet_type, combination, payout_amount
                FROM payouts WHERE race_id = ?
            """, (race['race_id'],)).fetchall()
            payout_map = {(p['bet_type'], p['combination']): p['payout_amount'] for p in payouts}

            # 各券種の的中チェック（三連単はスキップ）
            for bt, bt_bets in all_bets.items():
                if bt == '三連単':
                    continue  # 三連単は非公開

                for b in bt_bets:
                    amount = b.get('amount', 100)
                    hns = b.get('horse_numbers', [])
                    detail_str = b.get('detail', b.get('bet_detail', ''))
                    total_bets += 1
                    total_invested += amount

                    is_hit = False
                    actual_payout = 0

                    if bt == '単勝' and len(hns) >= 1:
                        if finish_map.get(hns[0], 99) == 1:
                            is_hit = True
                            actual_payout = payout_map.get(('単勝', str(hns[0])), 0)
                    elif bt == '複勝' and len(hns) >= 1:
                        if finish_map.get(hns[0], 99) <= 3:
                            is_hit = True
                            actual_payout = payout_map.get(('複勝', str(hns[0])), 0)
                    elif bt == 'ワイド' and len(hns) >= 2:
                        combo = '-'.join(str(h) for h in sorted(hns))
                        if all(finish_map.get(h, 99) <= 3 for h in hns):
                            is_hit = True
                            actual_payout = payout_map.get(('ワイド', combo), 0)
                    elif bt == '馬連' and len(hns) >= 2:
                        combo = '-'.join(str(h) for h in sorted(hns))
                        top2 = sorted([h for h, f in finish_map.items() if f <= 2])
                        if sorted(hns) == top2:
                            is_hit = True
                            actual_payout = payout_map.get(('馬連', combo), 0)
                    elif bt == '三連複' and len(hns) >= 3:
                        combo = '-'.join(str(h) for h in sorted(hns[:3]))
                        top3 = sorted([h for h, f in finish_map.items() if f <= 3])
                        if sorted(hns[:3]) == top3:
                            is_hit = True
                            actual_payout = payout_map.get(('三連複', combo), 0)

                    if is_hit and actual_payout > 0:
                        payout_val = int(actual_payout * (amount / 100))
                        total_payout += payout_val
                        total_hits += 1
                        hit_details.append({
                            'venue': race['venue'],
                            'race_number': race['race_number'],
                            'race_name': race['race_name'],
                            'grade': race['grade'] or '',
                            'confidence': race['confidence'],
                            'type': bt,
                            'detail': detail_str,
                            'invested': amount,
                            'payout': payout_val,
                            'roi': payout_val / amount * 100,
                        })

    # ── ツイート生成 ──
    hit_rate = total_hits / total_bets * 100 if total_bets > 0 else 0
    roi = total_payout / total_invested * 100 if total_invested > 0 else 0
    profit = total_payout - total_invested
    honmei_rate = honmei_top3 / honmei_total * 100 if honmei_total > 0 else 0

    if hit_details:
        # ── 的中あり → インパクト版 ──
        # ベスト的中をハイライト
        best = sorted(hit_details, key=lambda x: x['payout'], reverse=True)

        tweet = f"🎯 AI競馬 {date_label} 的中速報\n\n"

        # ベスト3的中
        for i, h in enumerate(best[:3]):
            emoji = "🥇" if i == 0 else "🥈" if i == 1 else "🥉"
            grade_str = f" [{h['grade']}]" if h['grade'] else ""
            tweet += f"{emoji} {h['venue']}{h['race_number']}R{grade_str}\n"
            tweet += f"  {h['type']} {h['detail']} → ¥{h['payout']:,}\n"

        tweet += f"\n━━ 本日の成績 ━━\n"
        tweet += f"◎複勝率: {honmei_rate:.0f}% ({honmei_top3}/{honmei_total})\n"
        tweet += f"的中: {total_hits}/{total_bets}件\n"

        # ROIに応じた表現
        if roi >= 200:
            tweet += f"💰 回収率: {roi:.0f}% 🔥🔥🔥\n"
            tweet += f"収支: +{profit:,}円\n"
        elif roi >= 100:
            tweet += f"💰 回収率: {roi:.0f}% 📈\n"
            tweet += f"収支: +{profit:,}円\n"
        else:
            tweet += f"回収率: {roi:.0f}%\n"
            tweet += f"収支: {profit:,}円\n"

        # CTA
        tweet += f"\n買い目は今朝のツイートで事前公開済み📋\n"

        # ハッシュタグ（ベストのレース名）
        tweet += f"\n{data_credit(short=True)}\n"
        tweet += "#AI競馬 #競馬予想"
        if best[0]['grade']:
            tag = best[0]['race_name'].replace(' ', '').replace('　', '')
            tweet += f" #{tag}"

    else:
        # ── 的中なし ──
        tweet = f"📊 AI競馬 {date_label} 結果\n\n"
        tweet += f"◎複勝率: {honmei_rate:.0f}% ({honmei_top3}/{honmei_total})\n"
        tweet += f"買い目的中: {total_hits}/{total_bets}件\n\n"

        if honmei_rate >= 60:
            tweet += "◎は安定していたものの買い目が裏目に。\n"
            tweet += "配当研究を継続します💪\n"
        else:
            tweet += "展開が合わず不調の1日。\n"
            tweet += "データを蓄積して精度向上に努めます💪\n"

        tweet += f"\n次回も買い目を朝に事前公開します📋\n"
        tweet += f"\n{data_credit(short=True)}\n"
        tweet += "#AI競馬 #競馬予想"

    # ファクトチェック
    if not fact_check_tweet(tweet):
        print("🚫 ファクトチェック不合格のため投稿を中止します")
        return

    print(f"\n📊 ファクトチェック詳細:")
    print(f"  対象: {races_analyzed}レース")
    print(f"  ◎成績: 勝率{honmei_win}/{honmei_total} 複勝率{honmei_top3}/{honmei_total}")
    print(f"  的中: {total_hits}/{total_bets}")
    print(f"  投資{total_invested:,}円 / 回収{total_payout:,}円 / ROI={roi:.1f}%")
    if hit_details:
        print(f"\n  🎯 ベスト的中:")
        for h in sorted(hit_details, key=lambda x: x['payout'], reverse=True)[:5]:
            print(f"    {h['venue']}{h['race_number']}R {h['type']} {h['detail']} → ¥{h['payout']:,} (ROI {h['roi']:.0f}%)")

    client = None
    threads_client = load_threads_client()
    if not args.dry_run:
        client = load_x_client()
        if not client:
            return

    post_tweet(client, tweet, dry_run=args.dry_run, threads_client=threads_client)


# ─── オッズ確定＋最終見解 ───
def cmd_odds_flash(args):
    """オッズ確定後の最終見解ツイート"""
    date_str = args.date
    dt = datetime.strptime(date_str, "%Y%m%d")
    weekday = ["月", "火", "水", "木", "金", "土", "日"][dt.weekday()]
    date_label = f"{dt.month}/{dt.day}({weekday})"
    date_hyphen = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    # 🛡 時間ガード: odds_flash は朝のオッズ確定後 (9:00-10:30 JST) 専用
    # 午後の誤発火は無意味 (post_predict が後で本投稿するため)
    if not os.environ.get("SKIP_TIME_GUARD"):
        now = now_jst()
        if now.strftime("%Y%m%d") == date_str and (now.hour < 9 or now.hour >= 11):
            print(f"⚠️ odds_flash 時間ガード: {now.strftime('%H:%M JST')} は対象外 (9:00-10:30 のみ)")
            return

    with get_db() as conn:
        races = conn.execute("""
            SELECT ra.race_id, ra.race_name, ra.venue, ra.grade,
                   pc.predictions_json
            FROM races ra
            JOIN predictions_cache pc ON ra.race_id = pc.race_id
            WHERE (ra.race_date = ? OR ra.race_date = ?)
            AND ra.race_number = 11
            ORDER BY ra.venue
        """, (date_str, date_hyphen)).fetchall()

    if not races:
        print(f"❌ {date_str} の11R予測データがありません")
        return

    tweets = []
    for race in races:
        preds = json.loads(race['predictions_json']) if race['predictions_json'] else []
        if not preds:
            continue

        rname = race['race_name']
        venue = race['venue']
        grade = f" [{race['grade']}]" if race['grade'] else ""

        # AI勝率順でTOP3
        top3 = sorted(preds, key=lambda x: x.get('pred_win_pct', 0), reverse=True)[:3]

        tweet = f"📊 オッズ確定！最終見解\n\n"
        tweet += f"{venue}11R {rname}{grade}\n\n"

        medals = ['🥇', '🥈', '🥉']
        for i, p in enumerate(top3):
            win_pct = p.get('pred_win_pct', 0)
            odds = p.get('odds_win', 0)
            name = p.get('horse_name', '?')
            pop = p.get('popularity', '?')
            tweet += f"{medals[i]} {name}\n"
            tweet += f"  AI勝率{win_pct}% / {odds}倍({pop}人気)\n"

        # 妙味判定: AI勝率が高いのにオッズが高い馬
        for p in top3:
            win_pct = p.get('pred_win_pct', 0)
            odds = p.get('odds_win', 0)
            if win_pct > 0 and odds > 0:
                ev = (win_pct / 100) * odds
                if ev > 1.2:
                    tweet += f"\n💎 {p['horse_name']}は妙味あり！\n"
                    break

        tweet += f"\n全レース予想はnoteで👇\n"
        tweet += f"{NOTE_URL}\n"
        tweet += f"#競馬予想 #AI予想"

        # ファクトチェック
        print(f"\n📊 ファクトチェック: {venue} {rname}")
        for i, p in enumerate(top3):
            print(f"  {medals[i]} {p.get('horse_name','?')}: "
                  f"AI勝率{p.get('pred_win_pct',0)}% / "
                  f"オッズ{p.get('odds_win',0)}倍 / "
                  f"{p.get('popularity','?')}人気")
        if not fact_check_tweet(tweet):
            print("🚫 ファクトチェック不合格のため投稿を中止します")
            return

        tweets.append(tweet)

    if not tweets:
        print("❌ 投稿するデータがありません")
        return

    client = None
    threads_client = load_threads_client()
    if not args.dry_run:
        client = load_x_client()
        if not client:
            return

    # 各レースを個別ツイートとして投稿
    for tweet in tweets:
        post_tweet(client, tweet, dry_run=args.dry_run, threads_client=threads_client)


# ─── 朝ツイート（平日7:30） ───
def cmd_morning(args):
    """平日朝: 今週末メインレースのコース分析（曜日別テーマ・日付動的表現）

    🛡 時間ガード (2026-05-25 追加 / 同日 +1h 拡大):
    cmd_morning は 朝 7:00-11:59 JST 専用。GitHub Actions cron が
    1h+ 遅延発火することが頻発 (5/25 朝は 1h, 昼は 4.5h 遅延) するため、
    7:30 予定の cron が 8:30-9:30 に発火するケースは正常運転として許容。
    ただし 12時超え (= 大幅遅延) は朝コンテンツとして不適切なのでスキップ。
    SKIP_TIME_GUARD=1 で意図的にバイパス可能(テスト用)。
    """
    if not os.environ.get("SKIP_TIME_GUARD"):
        now = now_jst()
        if not (7 <= now.hour <= 11):
            print(f"⚠️ cmd_morning 時間ガード: {now.strftime('%H:%M JST')} は対象外 (7:00-11:59 のみ)")
            print(f"   バイパスするには環境変数 SKIP_TIME_GUARD=1 を設定。")
            return

    fetch_weekend_races()
    today = now_jst()
    dow = today.weekday()
    print(f"📅 朝投稿 JST曜日: {['月','火','水','木','金','土','日'][dow]}曜日")

    tweet, race_id = _build_weekday_post('morning', today)

    if not tweet:
        print("⚠️ 投稿コンテンツを生成できませんでした（レース未登録 or データ不足）→ 投稿スキップ")
        return

    # 5/27 fix: thread (list[str]) も受け取れるよう helper を使う
    if not fact_check_tweet_or_thread(tweet):
        print("🚫 ファクトチェック不合格のため投稿を中止します")
        return

    client = None
    threads_client = load_threads_client()
    if not args.dry_run:
        client = load_x_client()
        if not client:
            return

    post_tweet(client, tweet, dry_run=args.dry_run, threads_client=threads_client, race_id=race_id)



# ─── 夜ツイート（平日20:00） ───
def cmd_evening(args):
    """平日夜: 今週末メインレースの総合プレビュー（曜日別テーマ・日付動的表現）

    🛡 時間ガード (2026-05-25 追加 / 同日 +1h 拡大):
    cmd_evening は 19:00-23:59 JST 専用。GitHub Actions cron が
    1-3h 遅延発火するケースは正常運転として許容。
    SKIP_TIME_GUARD=1 で意図的にバイパス可能(テスト用)。
    """
    if not os.environ.get("SKIP_TIME_GUARD"):
        now = now_jst()
        if not (19 <= now.hour <= 23):
            print(f"⚠️ cmd_evening 時間ガード: {now.strftime('%H:%M JST')} は対象外 (19:00-23:59 のみ)")
            print(f"   バイパスするには環境変数 SKIP_TIME_GUARD=1 を設定。")
            return

    fetch_weekend_races()
    today = now_jst()
    dow = today.weekday()
    print(f"📅 夜投稿 JST曜日: {['月','火','水','木','金','土','日'][dow]}曜日")

    tweet, race_id = _build_weekday_post('evening', today)

    if not tweet:
        print("⚠️ 投稿コンテンツを生成できませんでした → 投稿スキップ")
        return

    # 5/27 fix: thread (list[str]) も受け取れるよう helper を使う
    if not fact_check_tweet_or_thread(tweet):
        print("🚫 ファクトチェック不合格のため投稿を中止します")
        return

    client = None
    threads_client = load_threads_client()
    if not args.dry_run:
        client = load_x_client()
        if not client:
            return

    post_tweet(client, tweet, dry_run=args.dry_run, threads_client=threads_client, race_id=race_id)



def main():
    parser = argparse.ArgumentParser(
        description="🐦 EQUINOX Lab — X自動投稿"
    )
    subparsers = parser.add_subparsers(dest="command")

    # predict
    p_pred = subparsers.add_parser("predict", help="メインレース予想を投稿")
    p_pred.add_argument("--date", required=True, help="対象日 (YYYYMMDD)")
    p_pred.add_argument("--dry-run", action="store_true", help="投稿せずプレビュー")

    # results
    p_res = subparsers.add_parser("results", help="的中結果を投稿")
    p_res.add_argument("--date", required=True, help="対象日 (YYYYMMDD)")
    p_res.add_argument("--dry-run", action="store_true", help="投稿せずプレビュー")

    # weekday
    p_week = subparsers.add_parser("weekday", help="平日コンテンツを投稿")
    p_week.add_argument("--dry-run", action="store_true", help="投稿せずプレビュー")

    # answer_check
    p_ans = subparsers.add_parser("answer_check", help="全レース答え合わせを投稿")
    p_ans.add_argument("--date", required=True, help="対象日 (YYYYMMDD)")
    p_ans.add_argument("--dry-run", action="store_true", help="投稿せずプレビュー")

    # weekly_review
    p_rev = subparsers.add_parser("weekly_review", help="週間ROIレビューを投稿")
    p_rev.add_argument("--dry-run", action="store_true", help="投稿せずプレビュー")

    # hit_flash（的中速報）
    p_hit = subparsers.add_parser("hit_flash", help="的中速報ポスト")
    p_hit.add_argument("--date", required=True, help="対象日 (YYYYMMDD)")
    p_hit.add_argument("--dry-run", action="store_true", help="投稿せずプレビュー")

    # odds_flash（オッズ確定＋最終見解）
    p_odds = subparsers.add_parser("odds_flash", help="オッズ確定＋最終見解")
    p_odds.add_argument("--date", required=True, help="対象日 (YYYYMMDD)")
    p_odds.add_argument("--dry-run", action="store_true", help="投稿せずプレビュー")

    # morning（朝ツイート）
    p_morning = subparsers.add_parser("morning", help="平日朝のデータTipsツイート")
    p_morning.add_argument("--dry-run", action="store_true", help="投稿せずプレビュー")

    # evening（夜ツイート）
    p_evening = subparsers.add_parser("evening", help="平日夜のツイート")
    p_evening.add_argument("--dry-run", action="store_true", help="投稿せずプレビュー")

    # fetch_races（来週末レース取得）
    subparsers.add_parser("fetch_races", help="来週末の11Rをスクレイピングして登録")

    # preview（平日15テンプレを実投稿せずに表示）
    p_prev = subparsers.add_parser("preview", help="月-金 × 朝/昼/夜 = 15テンプレを表示")
    p_prev.add_argument("--monday", help="基準月曜の日付 (YYYYMMDD)、省略時は今週の月曜")

    args = parser.parse_args()
    init_db()

    # fetch_racesはtweepy不要
    if args.command == "fetch_races":
        fetch_weekend_races()
        return

    if not HAS_TWEEPY and not getattr(args, "dry_run", False):
        print("❌ tweepy がインストールされていません")
        print("   pip install tweepy")
        return

    if args.command == "predict":
        cmd_predict(args)
    elif args.command == "results":
        cmd_results(args)
    elif args.command == "weekday":
        cmd_weekday(args)
    elif args.command == "answer_check":
        cmd_answer_check(args)
    elif args.command == "weekly_review":
        cmd_weekly_review(args)
    elif args.command == "hit_flash":
        cmd_hit_flash(args)
    elif args.command == "odds_flash":
        cmd_odds_flash(args)
    elif args.command == "morning":
        cmd_morning(args)
    elif args.command == "evening":
        cmd_evening(args)
    elif args.command == "fetch_races":
        fetch_weekend_races()
    elif args.command == "preview":
        cmd_preview(args)
    else:
        parser.print_help()


def cmd_preview(args):
    """月-金 × 朝/昼/夜 の15テンプレートを実投稿せずに表示する。"""
    from datetime import date as _date, timedelta as _td
    if getattr(args, 'monday', None):
        s = args.monday
        base = _date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        # 月曜以外を渡された場合は最寄り月曜に補正
        if base.weekday() != 0:
            base = base - _td(days=base.weekday())
    else:
        today = now_jst().date()
        base = today - _td(days=today.weekday())  # 今週の月曜
    print(f"\n📋 平日15テンプレートのプレビュー (月曜基準: {base})\n")

    days = '月火水木金'
    slots = [('morning', '7:30'), ('weekday', '12:30'), ('evening', '20:00')]

    fetch_weekend_races()  # DB に来週末データを補充
    for i in range(5):
        d = base + _td(days=i)
        for slot, hhmm in slots:
            class _A:
                pass
            today_dt = datetime(d.year, d.month, d.day, int(hhmm.split(':')[0]),
                                tzinfo=timezone(timedelta(hours=9)))
            tweet = _build_weekday_post(slot, today_dt)
            print('=' * 64)
            print(f"  {d} ({days[i]}) {hhmm} [{slot}]")
            print('-' * 64)
            print(tweet or '<生成不可>')
            print()


if __name__ == "__main__":
    main()

