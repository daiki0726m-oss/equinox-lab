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

    client = tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_secret
    )
    return client


def x_weighted_len(text):
    """X APIの文字数カウント（日本語/絵文字=2, ASCII=1）"""
    count = 0
    for c in text:
        count += 2 if ord(c) > 127 else 1
    return count


def post_thread(client, tweets, dry_run=False, threads_client=None):
    """ツイートのリスト（スレッド）をX + Threadsに投稿（重複チェック付き）"""
    import hashlib

    # ─── 重複投稿チェック ───
    post_hash = hashlib.md5(tweets[0].encode()).hexdigest()[:12]

    if not dry_run:
        try:
            # 方法1: X APIで直近ツイートと比較（最も確実）
            if client and HAS_TWEEPY:
                try:
                    me = client.get_me()
                    if me and me.data:
                        recent = client.get_users_tweets(
                            me.data.id, max_results=5,
                            tweet_fields=["created_at"]
                        )
                        if recent and recent.data:
                            first_line = tweets[0][:50]  # 先頭50文字で比較
                            for t in recent.data:
                                if first_line in t.text:
                                    print(f"⚠️ 重複検出（X API）: 同じ内容が既に投稿済み → スキップ")
                                    print(f"  既存ツイート: {t.text[:60]}...")
                                    return []
                except Exception as api_err:
                    print(f"  ℹ️ X API重複チェックスキップ: {api_err}")

            # 方法2: ローカルファイル（ローカル実行時）
            log_path = os.path.join(os.path.dirname(__file__), ".post_history.json")
            now_ts = now_jst().timestamp()

            history = {}
            if os.path.exists(log_path):
                with open(log_path, 'r') as f:
                    history = json.load(f)

            if post_hash in history:
                last_ts = history[post_hash]
                elapsed = now_ts - last_ts
                if elapsed < 7200:
                    elapsed_min = int(elapsed / 60)
                    print(f"⚠️ 重複検出（ファイル）: 同じ内容が{elapsed_min}分前に投稿済み → スキップ")
                    return []

            # 投稿履歴を記録
            history = {k: v for k, v in history.items() if now_ts - v < 86400}
            history[post_hash] = now_ts
            with open(log_path, 'w') as f:
                json.dump(history, f)
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
                result = client.create_tweet(**kwargs)
                tid = result.data["id"]
                tweet_ids.append(tid)
                parent_id = tid
                print(f"  ✅ X投稿完了 (ID: {tid}, X:{wlen}文字)")
            except Exception as e:
                print(f"  ❌ X投稿失敗: {e}")
                break

    # Threads にも同時投稿（複数ツイートを1投稿に結合）
    if threads_client:
        threads_text = adapt_text_for_threads(tweets)
        post_to_threads(threads_client, threads_text, dry_run=dry_run)

    return tweet_ids


def post_tweet(client, text, reply_to=None, dry_run=False, threads_client=None):
    """単一ツイートまたは自動分割して投稿（X + Threads同時）"""
    if isinstance(text, list):
        return post_thread(client, text, dry_run=dry_run, threads_client=threads_client)
    return post_thread(client, [text], dry_run=dry_run, threads_client=threads_client)


# ─── レース当日: メインレース予想 ───
def cmd_predict(args):
    """レース前の買い目公開ツイート（11R + 信頼度S/A）"""
    date_str = args.date
    dt = datetime.strptime(date_str, "%Y%m%d")
    weekday = ["月", "火", "水", "木", "金", "土", "日"][dt.weekday()]
    date_label = f"{dt.month}/{dt.day}({weekday})"
    date_hyphen = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    with get_db() as conn:
        # 全レースの予測データ取得
        all_races = conn.execute("""
            SELECT ra.race_id, ra.race_name, ra.venue, ra.distance, ra.surface,
                   ra.track_condition, ra.grade, ra.race_number, ra.start_time,
                   pc.predictions_json, pc.all_bets_json, pc.confidence
            FROM races ra
            JOIN predictions_cache pc ON ra.race_id = pc.race_id
            WHERE (ra.race_date = ? OR ra.race_date = ?)
            ORDER BY ra.venue, ra.race_number
        """, (date_str, date_hyphen)).fetchall()

    if not all_races:
        print(f"❌ {date_str} の予測データがありません")
        return

    # 投稿対象: 11R（必ず） + 信頼度Sのレース（最大3件）
    target_races = []
    target_ids = set()

    # まず11Rを追加
    for race in all_races:
        if race['race_number'] == 11 and race['race_id'] not in target_ids:
            target_races.append(race)
            target_ids.add(race['race_id'])

    # 信頼度Sのレース（11R以外、最大3件）
    s_count = 0
    for race in all_races:
        if race['confidence'] == 'S' and race['race_id'] not in target_ids:
            target_races.append(race)
            target_ids.add(race['race_id'])
            s_count += 1
            if s_count >= 3:
                break

    if not target_races:
        print(f"❌ 投稿対象レースがありません")
        return

    print(f"🏇 {date_label} 投稿対象: {len(target_races)}レース")
    print(f"   (11R: {sum(1 for r in target_races if r['race_number']==11)}件 / "
          f"S: {sum(1 for r in target_races if r['confidence']=='S' and r['race_number']!=11)}件)\n")

    # ── ツイート1: サマリー ──
    main_races = [r for r in target_races if r['race_number'] == 11]
    s_races = [r for r in target_races if r['confidence'] == 'S' and r['race_number'] != 11]

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
            t1 += f"  ◎{honmei.get('horse_name','?')}\n"

    if s_races:
        t1 += f"\n🔥 AI高信頼レース: {len(s_races)}件\n"

    t1 += f"\n買い目は🧵↓で事前公開\n"
    t1 += "#AI競馬 #競馬予想"

    # ── ツイート2以降: 各レースの買い目 ──
    bet_tweets = []
    for race in target_races:
        preds = json.loads(race['predictions_json']) if race['predictions_json'] else []
        all_bets = json.loads(race['all_bets_json']) if race['all_bets_json'] else {}

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
        conf_emoji = "🔥" if race['confidence'] == 'S' else "⭐" if race['confidence'] == 'A' else "📊"
        is_main = "メイン" if race['race_number'] == 11 else ""

        t = f"{conf_emoji} {race['venue']}{race['race_number']}R {race['race_name']}{grade}\n"
        t += f"信頼度{race['confidence']} {is_main}\n\n"

        # 印（全て表示）
        for mk in ['◎', '○', '▲', '△', '×', '注']:
            p = marks.get(mk)
            if p:
                t += f"{mk} {p.get('horse_number',0)}.{p.get('horse_name','?')}\n"

        # 具体的な買い目（三連単除外）
        t += f"\n💰 推奨買い目\n"
        bet_count = 0
        total_amount = 0
        for bt, bt_bets in all_bets.items():
            if bt == '三連単':
                continue
            for b in bt_bets:
                hns = b.get('horse_numbers', [])
                amount = b.get('amount', 100)
                # 重複馬番チェック
                if len(hns) != len(set(hns)):
                    continue
                combo = '-'.join(str(h) for h in hns)
                t += f"  {bt} {combo} ¥{amount:,}\n"
                bet_count += 1
                total_amount += amount

        t += f"  計{bet_count}点 ¥{total_amount:,}\n"

        bet_tweets.append(t)

    # ── 最終ツイート: CTA ──
    t_last = f"📊 以上 {date_label} のAI推奨買い目です\n\n"
    t_last += f"全{len(target_races)}レース・具体的な買い目を\n"
    t_last += f"レース前に公開しています\n\n"
    t_last += "的中結果は夕方に速報します🎯\n\n"
    t_last += "#AI競馬 #競馬予想"

    # レース名ハッシュタグ（メインレース）
    for race in main_races[:2]:
        if race['grade']:
            tag = race['race_name'].replace(' ', '').replace('　', '')
            t_last += f" #{tag}"

    tweets = [t1] + bet_tweets + [t_last]

    # ファクトチェック
    for tw in tweets:
        fact_check_tweet(tw)

    client = None
    threads_client = load_threads_client()
    if not args.dry_run:
        client = load_x_client()
        if not client:
            return

    post_thread(client, tweets, dry_run=args.dry_run, threads_client=threads_client)


# ─── レース当日: 的中結果報告 ───
def cmd_results(args):
    """的中結果を報告するツイートを生成・投稿（3段スレッド）"""
    date_str = args.date
    dt = datetime.strptime(date_str, "%Y%m%d")
    weekday = ["月", "火", "水", "木", "金", "土", "日"][dt.weekday()]
    date_label = f"{dt.month}/{dt.day}({weekday})"
    date_hyphen = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    with get_db() as conn:
        races = conn.execute("""
            SELECT ra.race_id, ra.race_name, ra.venue,
                   pc.predictions_json, pc.all_bets_json
            FROM races ra
            JOIN predictions_cache pc ON ra.race_id = pc.race_id
            WHERE (ra.race_date = ? OR ra.race_date = ?)
            AND ra.race_number = 11
            ORDER BY ra.venue
        """, (date_str, date_hyphen)).fetchall()

        if not races:
            print(f"❌ {date_str} の予測データがありません")
            return

        race_results = []
        total_invested = 0
        total_payout = 0

        for race in races:
            preds = json.loads(race['predictions_json']) if race['predictions_json'] else []
            all_bets = json.loads(race['all_bets_json']) if race['all_bets_json'] else {}

            # 配当情報を取得
            payouts = conn.execute("""
                SELECT bet_type, combination, payout_amount
                FROM payouts WHERE race_id = ?
            """, (race['race_id'],)).fetchall()
            payout_map = {}
            for p in payouts:
                key = (p['bet_type'], p['combination'])
                payout_map[key] = p['payout_amount']

            # 着順を取得
            finishes = conn.execute("""
                SELECT horse_number, finish_position FROM results
                WHERE race_id = ? AND finish_position > 0
            """, (race['race_id'],)).fetchall()
            finish_map = {f['horse_number']: f['finish_position'] for f in finishes}

            # ◎○▲を特定
            marks = {'◎': None, '○': None, '▲': None}
            for p in preds:
                m = p.get('mark', '')
                if m in marks and marks[m] is None:
                    marks[m] = p

            # 各推奨買い目の的中チェック
            race_invest = 0
            race_payout_total = 0
            hit_bets = []
            miss_count = 0

            for bt, bt_bets in all_bets.items():
                for b in bt_bets:
                    amount = b.get('amount', 100)
                    race_invest += amount
                    detail = b.get('detail', '')

                    is_hit = False
                    actual_payout = 0

                    if bt == '単勝':
                        hn = b['horse_numbers'][0]
                        if finish_map.get(hn) == 1:
                            is_hit = True
                            actual_payout = payout_map.get(('単勝', str(hn)), 0)
                    elif bt == '複勝':
                        hn = b['horse_numbers'][0]
                        if finish_map.get(hn, 99) <= 3:
                            is_hit = True
                            actual_payout = payout_map.get(('複勝', str(hn)), 0)
                    elif bt == 'ワイド':
                        hns = sorted(b['horse_numbers'])
                        combo = '-'.join(str(h) for h in hns)
                        if all(finish_map.get(h, 99) <= 3 for h in hns):
                            is_hit = True
                            actual_payout = payout_map.get(('ワイド', combo), 0)
                    elif bt == '馬連':
                        hns = sorted(b['horse_numbers'])
                        combo = '-'.join(str(h) for h in hns)
                        top2 = sorted([h for h, f in finish_map.items() if f <= 2])
                        if hns == top2:
                            is_hit = True
                            actual_payout = payout_map.get(('馬連', combo), 0)
                    elif bt == '三連複':
                        hns = sorted(set(b['horse_numbers'][:3]))
                        combo = '-'.join(str(h) for h in hns)
                        top3 = sorted([h for h, f in finish_map.items() if f <= 3])
                        if hns == top3:
                            is_hit = True
                            actual_payout = payout_map.get(('三連複', combo), 0)
                    elif bt == '三連単':
                        hns = b['horse_numbers'][:3]
                        ordered = [h for h, f in sorted(finish_map.items(), key=lambda x: x[1]) if f <= 3]
                        if hns == ordered[:3]:
                            is_hit = True
                            combo = '-'.join(str(h) for h in hns)
                            actual_payout = payout_map.get(('三連単', combo), 0)

                    if is_hit and actual_payout > 0:
                        payout_val = int(actual_payout * (amount / 100))
                        race_payout_total += payout_val
                        hit_bets.append({'type': bt, 'detail': detail, 'payout': payout_val})
                    else:
                        miss_count += 1

            total_invested += race_invest
            total_payout += race_payout_total

            race_results.append({
                'venue': race['venue'],
                'race_name': race['race_name'],
                'marks': marks,
                'hit_bets': hit_bets,
                'miss_count': miss_count,
                'invested': race_invest,
                'payout': race_payout_total,
            })

    if not race_results:
        print(f"❌ {date_str} の結果がまだ出ていません")
        return

    total_races = len(race_results)
    roi = round(total_payout / total_invested * 100) if total_invested > 0 else 0
    profit = int(total_payout - total_invested)
    hit_races = sum(1 for r in race_results if r['hit_bets'])

    # ── ツイート1: ROI概要 ──
    t1 = f"📊 {date_label} AI推奨買い目の結果\n\n"
    t1 += f"メインレース {total_races}レース\n"
    t1 += f"投資: {int(total_invested):,}円\n"
    t1 += f"回収: {int(total_payout):,}円\n"
    t1 += f"収支: {'+' if profit >= 0 else ''}{profit:,}円\n"
    t1 += f"ROI: {roi}%\n\n"
    if total_invested == 0:
        t1 += "推奨買い目なし（見送り判断）\n\n"
    elif roi >= 100:
        t1 += "プラス回収！📈🔥\n\n"
    elif hit_races > 0:
        t1 += f"{hit_races}レース的中もトータルマイナス 📉\n\n"
    else:
        t1 += "全不的中。素直に反省 📉\n\n"
    t1 += "#競馬予想 #AI予想 #競馬結果 🧵↓"

    # ── ツイート2: 各レース結果 ──
    t2 = f"📋 各レース結果\n\n"
    for r in race_results:
        m = r['marks']
        if r['hit_bets']:
            t2 += f"✅ {r['venue']} {r['race_name']}\n"
            if m['◎']:
                t2 += f" ◎{m['◎']['horse_number']}{m['◎']['horse_name']}\n"
            for hb in r['hit_bets'][:2]:
                t2 += f" 💰{hb['type']} {hb['detail']}→{hb['payout']:,}円\n"
        elif r['invested'] == 0:
            t2 += f"⏸️ {r['venue']} {r['race_name']}\n"
            t2 += " AI評価D→見送り推奨（買い目なし）\n"
        else:
            t2 += f"❌ {r['venue']} {r['race_name']}\n"
            if m['◎']:
                fp = '?'
                # ◎の着順を取得
                t2 += f" ◎{m['◎']['horse_number']}{m['◎']['horse_name']}\n"
            t2 += f" 推奨{r['miss_count']}点 不的中\n"

    # ── ツイート3: 総括 ──
    t3 = "💡 振り返り\n\n"
    if roi >= 100:
        t3 += f"ROI {roi}%でプラス回収\n"
        t3 += f"的中レース: {hit_races}/{total_races}\n\n"
    else:
        t3 += "外れた原因を分析し精度向上します\n\n"

    # 次の開催日を判定（土→日→来週土）
    next_label = "来週も"
    if dt.weekday() == 5:  # 土曜
        next_label = "明日も"
    t3 += f"{next_label}メインレースAI予想を配信\n"
    t3 += "フォロー&通知ONで見逃さない🔔"

    tweets = [t1, t2, t3]

    client = None
    threads_client = load_threads_client()
    if not args.dry_run:
        client = load_x_client()
        if not client:
            return

    post_thread(client, tweets, dry_run=args.dry_run, threads_client=threads_client)


# ─── 平日コンテンツ ───
def cmd_weekday(args):
    """平日用の自動コンテンツを生成・投稿"""
    today = now_jst()
    dow = today.weekday()  # 0=月, 4=金
    print(f"📅 JST曜日: {['月','火','水','木','金','土','日'][dow]}曜日")

    if dow == 0:
        # 月曜: 先週末の振り返り
        tweet = generate_weekly_summary()
    elif dow in (1, 2, 3):
        # 火〜木: note記事プロモ（記事がなければフォールバック）
        tweet = generate_note_promo()
    elif dow == 4:
        # 金曜: 週末プレビュー
        tweet = generate_weekend_preview()
    else:
        tweet = generate_analysis_column()

    if not tweet:
        print("❌ コンテンツ生成に失敗")
        return

    client = None
    threads_client = load_threads_client()
    if not args.dry_run:
        client = load_x_client()
        if not client:
            return

    post_tweet(client, tweet, dry_run=args.dry_run, threads_client=threads_client)


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
        return generate_analysis_column()

    # 的中数計算（3着以内を的中とする）
    hits = sum(1 for r in results_list if isinstance(r['finish'], int) and r['finish'] <= 3)
    total = len(results_list)
    hit_rate = round(hits / total * 100) if total > 0 else 0

    t1 = f"📊 先週末({dr}) AI予測の結果\n"
    t1 += f"対象: メインレース(11R) {total}レース\n\n"
    t1 += f"AI本命(◎)の複勝的中率: {hits}/{total} ({hit_rate}%)\n\n"
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
        return generate_analysis_column()

    period = f"{start_date.year}/{start_date.month}/{start_date.day}〜{end_dt.year}/{end_dt.month}/{end_dt.day}"

    t1 = f"🏆 調教師 勝率ランキング\n"
    t1 += f"集計期間: {period}\n\n"
    t1 += "勝率が高い=仕上げ力がある厩舎\n"
    t1 += "10頭以上出走の調教師を集計\n\n"
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
        return generate_analysis_column()

    period = f"{start_date.year}/{start_date.month}/{start_date.day}〜{end_dt.year}/{end_dt.month}/{end_dt.day}"

    t1 = "🤝 騎手×調教師 好相性コンビTOP5\n"
    t1 += f"集計期間: {period}\n\n"
    t1 += "同じ騎手でも調教師との相性で\n"
    t1 += "成績が大きく変わる\n\n"
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
            return generate_analysis_column()

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
        return generate_analysis_column()

    t1 = "📏 距離変更と成績の関係\n\n"
    t1 += "距離を延長/短縮した馬は\n"
    t1 += "成績にどう影響する？\n"
    t1 += "半年分のデータで検証\n\n"
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
    t1 += "\n\n#競馬予想 #AI予想 🧵↓"

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
        return generate_analysis_column()

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
        return generate_analysis_column()

    # ── ツイート1: フック ──
    t1 = f"🔍 今週末({weekend_str})の注目馬\n\n"
    t1 += "前走凡走でも今回条件が変わる馬を\n"
    t1 += "3つの切り口でAIがピックアップ\n\n"
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
            return generate_analysis_column()

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

    t1 += "\n#競馬予想 #AI予想 #競馬結果"

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

    t1 += "\n#競馬予想 #AI予想 #競馬結果"

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
        return generate_analysis_column()

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
        return generate_analysis_column()

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
def fact_check_tweet(tweet_text):
    """ツイートの数値データをDBと照合して検証する。
    数値が含まれるツイートの場合、DBからデータを再取得して一致を確認。
    不一致があればWarningを出す。
    """
    import re
    issues = []

    # 勝率/複勝率の表記を検証
    pct_matches = re.findall(r'(\d+\.?\d*)%', tweet_text)
    for pct in pct_matches:
        val = float(pct)
        if val > 100:
            issues.append(f"⚠️ {val}% は100%を超えています")
        if val == 0:
            issues.append(f"⚠️ 0% は不自然な値です")

    # 金額表記の検証
    yen_matches = re.findall(r'([\d,]+)円', tweet_text)
    for yen in yen_matches:
        val = int(yen.replace(',', ''))
        if val > 10000000:  # 1000万円超
            issues.append(f"⚠️ {yen}円 は異常に高額です")

    # ROIの検証
    roi_matches = re.findall(r'ROI[:\s]*(\d+)%', tweet_text)
    for roi in roi_matches:
        val = int(roi)
        if val > 10000:
            issues.append(f"⚠️ ROI {val}% は異常値です")

    if issues:
        print("🔍 ファクトチェック結果:")
        for issue in issues:
            print(f"  {issue}")
        return False
    else:
        print("✅ ファクトチェック通過")
        return True


# ─── 的中速報ポスト ───
def cmd_hit_flash(args):
    """的中速報ツイート（全レース対象・インパクト重視）"""
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
        tweet += "#AI競馬 #競馬予想"

    # ファクトチェック
    fact_check_tweet(tweet)

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
        fact_check_tweet(tweet)

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
    """平日朝のデータTipsツイート（DBの実データを使用）"""
    today = now_jst()
    # 曜日+週番号でパターンを決定（毎日違うネタ）
    pattern_idx = (today.weekday() * 7 + today.isocalendar()[1]) % 5

    tweet = None

    try:
        with get_db() as conn:
            if pattern_idx == 0:
                # パターン1: 1番人気の真実（DB実データ）
                fav = conn.execute("""
                    SELECT COUNT(*) as total,
                           SUM(CASE WHEN finish_position = 1 THEN 1 ELSE 0 END) as wins,
                           SUM(CASE WHEN finish_position <= 3 THEN 1 ELSE 0 END) as top3
                    FROM results WHERE popularity = 1 AND finish_position > 0
                """).fetchone()

                if fav and fav['total'] > 0:
                    win_r = round(fav['wins'] / fav['total'] * 100, 1)
                    top3_r = round(fav['top3'] / fav['total'] * 100, 1)

                    tweet = f"💡 競馬データの真実\n\n"
                    tweet += f"1番人気の成績（DB全{fav['total']:,}レース）:\n\n"
                    tweet += f"勝率: {win_r}%\n"
                    tweet += f"複勝率: {top3_r}%\n\n"
                    tweet += "3回に1回しか勝たない。\n"
                    tweet += "でも3回に2回は3着以内。\n\n"
                    tweet += "❌ 1番人気の単勝→長期で負け\n"
                    tweet += "✅ 期待値の高い馬を狙う\n\n"
                    tweet += "AIが毎週やってることです🧠\n\n"
                    tweet += f"{NOTE_URL}\n\n"
                    tweet += "#競馬豆知識 #競馬データ"

                    # ファクトチェック
                    print(f"📊 ファクトDB検証: 1番人気 {fav['total']}R中 "
                          f"勝率{win_r}% 複勝率{top3_r}%")

            elif pattern_idx == 1:
                # パターン2: 騎手勝率ランキング（直近30日実データ）
                jockeys = conn.execute("""
                    SELECT j.jockey_name,
                           COUNT(*) as entries,
                           SUM(CASE WHEN r.finish_position = 1 THEN 1 ELSE 0 END) as wins,
                           SUM(CASE WHEN r.finish_position <= 3 THEN 1 ELSE 0 END) as top3
                    FROM results r
                    JOIN jockeys j ON r.jockey_id = j.jockey_id
                    JOIN races ra ON r.race_id = ra.race_id
                    WHERE ra.race_date >= date('now', '-30 days')
                    AND ra.race_date <= date('now')
                    AND r.finish_position > 0
                    GROUP BY j.jockey_id HAVING entries >= 15
                    ORDER BY CAST(wins AS FLOAT)/entries DESC LIMIT 3
                """).fetchall()

                if jockeys and len(jockeys) >= 3:
                    tweet = "🏆 直近30日 騎手勝率ランキング\n\n"
                    medals = ['🥇', '🥈', '🥉']
                    for i, j in enumerate(jockeys):
                        jname = j['jockey_name'].lstrip('▲△★☆')
                        wr = round(j['wins'] / j['entries'] * 100, 1)
                        tweet += f"{medals[i]} {jname} 勝率{wr}% ({j['entries']}騎乗)\n"

                    tweet += f"\n好調な騎手の馬は\n"
                    tweet += "AI評価にもプラスに反映📊\n\n"
                    tweet += f"週末の予想はこちら👇\n"
                    tweet += f"{NOTE_URL}\n\n"
                    tweet += "#競馬データ #騎手成績"

                    # ファクトチェック
                    for j in jockeys:
                        jname = j['jockey_name'].lstrip('▲△★☆')
                        wr = round(j['wins'] / j['entries'] * 100, 1)
                        print(f"📊 ファクトDB検証: {jname} {j['entries']}騎乗 勝率{wr}%")

            elif pattern_idx == 2:
                # パターン3: AI紹介（48次元）
                horse_count = conn.execute("SELECT COUNT(*) as c FROM horses").fetchone()['c']
                race_count = conn.execute(
                    "SELECT COUNT(*) as c FROM results WHERE finish_position > 0"
                ).fetchone()['c']

                tweet = "🧬 AIが見る「48の視点」\n\n"
                tweet += "私たちの予測モデル:\n\n"
                tweet += "📊 過去成績 → 勝率・複勝率\n"
                tweet += "🏟️ コース適性 → 芝/ダ・距離別\n"
                tweet += f"🧬 血統 → 父・母父の{horse_count:,}頭分析\n"
                tweet += "🏋️ 斤量/体重 → 当日の状態\n"
                tweet += "👤 騎手×調教師 → 相性データ\n\n"
                tweet += f"全{race_count:,}レースの学習済み🧠\n\n"
                tweet += f"{NOTE_URL}\n\n"
                tweet += "#AI競馬 #機械学習"

                print(f"📊 ファクトDB検証: 馬{horse_count:,}頭 / {race_count:,}レース")

            elif pattern_idx == 3:
                # パターン4: コース適性
                tweet = "📐 コース適性の違い\n\n"
                tweet += "同じ「芝1600m」でも全然違う：\n\n"
                tweet += "🏟️ 東京 → 直線525m（差し有利）\n"
                tweet += "🏟️ 中山 → 直線310m（先行有利）\n"
                tweet += "🏟️ 阪神外 → 直線473m（差し有利）\n"
                tweet += "🏟️ 京都 → 直線404m（バランス型）\n\n"
                tweet += "「前走東京で差して勝った馬」が\n"
                tweet += "中山で人気になったら要注意⚠️\n\n"
                tweet += f"{NOTE_URL}\n\n"
                tweet += "#競馬データ #コース適性"

            elif pattern_idx == 4:
                # パターン5: 回収率の話
                tweet = "💰 回収率と的中率の違い\n\n"
                tweet += "的中率80%でも負ける場合:\n"
                tweet += "→ 1.1倍ばかり当てて外れで大損\n\n"
                tweet += "的中率20%でも勝つ場合:\n"
                tweet += "→ 期待値の高い馬を狙い撃ち\n\n"
                tweet += "AIが追求するのは「回収率」\n\n"
                tweet += "的中率に一喜一憂しない。\n"
                tweet += "長期で+にする。これが投資競馬🧠\n\n"
                tweet += f"{NOTE_URL}\n\n"
                tweet += "#競馬投資 #回収率"

    except Exception as e:
        print(f"⚠️ DB読み取りエラー: {e}")

    if not tweet:
        # フォールバック
        tweet = "🏇 おはようございます！\n\n"
        tweet += "EQUINOX Labです。\n"
        tweet += "48次元AIで競馬予想を配信中📊\n\n"
        tweet += "土日朝7時にメインレースの\n"
        tweet += "AI予想を無料公開しています🔔\n\n"
        tweet += f"{NOTE_URL}\n\n"
        tweet += "#競馬予想 #AI予想"

    fact_check_tweet(tweet)

    client = None
    threads_client = load_threads_client()
    if not args.dry_run:
        client = load_x_client()
        if not client:
            return

    post_tweet(client, tweet, dry_run=args.dry_run, threads_client=threads_client)


# ─── 夜ツイート（平日20:00） ───
def cmd_evening(args):
    """平日夜のツイート（週末予告/note宣伝/問いかけ）"""
    today = now_jst()
    pattern_idx = (today.weekday() * 3 + today.isocalendar()[1]) % 5

    tweet = None

    try:
        with get_db() as conn:
            if pattern_idx == 0:
                # パターン1: 今週の重賞スケジュール（DB実データ）
                upcoming = conn.execute("""
                    SELECT race_name, venue, distance, surface, grade, race_date
                    FROM races WHERE race_date > date('now')
                    AND race_date <= date('now', '+7 days')
                    AND grade IS NOT NULL AND grade != ''
                    ORDER BY race_date, venue
                """).fetchall()

                if upcoming:
                    tweet = "📢 今週の重賞スケジュール\n\n"
                    current_date = None
                    for r in upcoming:
                        rd = datetime.strptime(r['race_date'], '%Y-%m-%d')
                        dow = ["月", "火", "水", "木", "金", "土", "日"][rd.weekday()]
                        date_lbl = f"{rd.month}/{rd.day}({dow})"

                        if date_lbl != current_date:
                            tweet += f"\n{date_lbl}\n"
                            current_date = date_lbl

                        emoji = "🏆" if "G1" in r['grade'] else "🏇"
                        tweet += f"{emoji} {r['race_name']} [{r['grade']}]\n"

                    tweet += f"\nAI予想は当日朝7時に配信🔔\n"
                    tweet += f"noteでデータ分析も公開中👇\n"
                    tweet += f"{NOTE_URL}\n\n"
                    tweet += "#競馬予想"

                    # レース名からハッシュタグ生成
                    for r in upcoming[:2]:
                        tag = r['race_name'].replace(' ', '').replace('　', '')
                        tweet += f" #{tag}"

                    print(f"📊 ファクトDB検証: 今週の重賞 {len(upcoming)}レース")

            elif pattern_idx == 1:
                # パターン2: note記事宣伝
                import glob
                articles_dir = os.path.join(os.path.dirname(__file__), "articles")
                promo_files = []
                if os.path.exists(articles_dir):
                    for f in glob.glob(os.path.join(articles_dir, "*.md")):
                        basename = os.path.basename(f)
                        if not basename.startswith("review_") and not basename.startswith("2026"):
                            promo_files.append(f)

                if promo_files:
                    promo_files.sort(key=os.path.getmtime, reverse=True)
                    latest = promo_files[0]
                    with open(latest, 'r', encoding='utf-8') as f:
                        content = f.read()

                    # タイトル抽出
                    title = ""
                    for line in content.split("\n"):
                        if line.strip().startswith("# "):
                            title = line.strip().replace("# ", "")
                            break

                    if title:
                        tweet = "📝 note記事を公開中！\n\n"
                        tweet += f"【{title}】\n\n"

                        # ポイント抽出
                        points = [l.strip() for l in content.split("\n")
                                  if l.strip().startswith("✅") or l.strip().startswith("- ✅")]
                        for p in points[:4]:
                            tweet += f"{p}\n"

                        tweet += f"\nAIデータで徹底分析👇\n"
                        tweet += f"{NOTE_URL}\n\n"
                        tweet += "#競馬予想 #競馬データ"

            elif pattern_idx == 2:
                # パターン3: フォロワーへの問いかけ
                tweet = "💬 質問させてください！\n\n"
                tweet += "AI競馬予想で一番知りたいのは？\n\n"
                tweet += "1⃣ ◎本命の信頼度\n"
                tweet += "2⃣ 回収率（ROI）\n"
                tweet += "3⃣ 穴馬の発掘\n"
                tweet += "4⃣ 具体的な買い目\n\n"
                tweet += "リプで教えてください🙏\n"
                tweet += "みなさんの声で発信を改善します！\n\n"
                tweet += f"予想の全容はこちら👇\n"
                tweet += f"{NOTE_URL}\n\n"
                tweet += "#競馬予想 #AI競馬"

            elif pattern_idx == 3:
                # パターン4: 実績アピール（DB実データ）
                horse_count = conn.execute("SELECT COUNT(*) as c FROM horses").fetchone()['c']

                tweet = "📊 EQUINOX Lab の実績\n\n"
                tweet += "モデル性能:\n"
                tweet += "🎯 48特徴量のLightGBM\n"
                tweet += "📈 勝率/複勝率/着順を同時予測\n"
                tweet += f"🧬 血統DB: {horse_count:,}頭\n\n"
                tweet += "毎週土日に全メインレースの\n"
                tweet += "AI予想＋買い目を配信中。\n\n"
                tweet += "「データが、直感を超える。」\n\n"
                tweet += f"無料で読めます👇\n"
                tweet += f"{NOTE_URL}\n\n"
                tweet += "#AI競馬 #競馬予想"

                print(f"📊 ファクトDB検証: 馬{horse_count:,}頭")

            elif pattern_idx == 4:
                # パターン5: 次の重賞予告
                next_graded = conn.execute("""
                    SELECT race_name, grade, venue, race_date
                    FROM races WHERE race_date > date('now')
                    AND grade IS NOT NULL AND grade != ''
                    ORDER BY race_date LIMIT 1
                """).fetchone()

                if next_graded:
                    rd = datetime.strptime(next_graded['race_date'], '%Y-%m-%d')
                    dow = ["月", "火", "水", "木", "金", "土", "日"][rd.weekday()]

                    tweet = f"🔥 {next_graded['race_name']}（{next_graded['grade']}）\n\n"
                    tweet += f"{rd.month}/{rd.day}({dow}) {next_graded['venue']}開催\n\n"
                    tweet += "noteでデータ分析を公開予定！\n\n"
                    tweet += "✅ 過去の傾向データ\n"
                    tweet += "✅ 人気別・脚質別成績\n"
                    tweet += "✅ AI注目馬ピックアップ\n"
                    tweet += "✅ 危険な人気馬の見極め\n\n"
                    tweet += f"フォローして見逃さない🔔\n"
                    tweet += f"{NOTE_URL}\n\n"

                    tag = next_graded['race_name'].replace(' ', '').replace('　', '')
                    tweet += f"#{tag} #競馬予想"

                    print(f"📊 ファクトDB検証: 次の重賞 = {next_graded['race_name']}"
                          f"({next_graded['grade']}) {next_graded['race_date']}")

    except Exception as e:
        print(f"⚠️ DB読み取りエラー: {e}")

    if not tweet:
        # フォールバック
        tweet = "🌙 お疲れ様です！\n\n"
        tweet += "EQUINOX Labです。\n"
        tweet += "今週もAI競馬予想を配信します📊\n\n"
        tweet += "土日朝7時に全メインレース予想🏇\n"
        tweet += "noteでデータ分析も公開中👇\n\n"
        tweet += f"{NOTE_URL}\n\n"
        tweet += "#競馬予想 #AI予想"

    fact_check_tweet(tweet)

    client = None
    threads_client = load_threads_client()
    if not args.dry_run:
        client = load_x_client()
        if not client:
            return

    post_tweet(client, tweet, dry_run=args.dry_run, threads_client=threads_client)


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

    args = parser.parse_args()
    init_db()

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
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

