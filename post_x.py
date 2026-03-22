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
from datetime import datetime, timedelta, timezone

# JST タイムゾーン
JST = timezone(timedelta(hours=9))

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


def post_thread(client, tweets, dry_run=False):
    """ツイートのリスト（スレッド）を投稿"""
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
                print(f"  ✅ 投稿完了 (ID: {tid}, X:{wlen}文字)")
            except Exception as e:
                print(f"  ❌ 投稿失敗: {e}")
                break

    return tweet_ids


def post_tweet(client, text, reply_to=None, dry_run=False):
    """単一ツイートまたは自動分割して投稿"""
    if isinstance(text, list):
        return post_thread(client, text, dry_run=dry_run)
    return post_thread(client, [text], dry_run=dry_run)


# ─── レース当日: メインレース予想 ───
def cmd_predict(args):
    """メインレース(11R)の予想ツイートを生成・投稿（3段スレッド）"""
    date_str = args.date
    dt = datetime.strptime(date_str, "%Y%m%d")
    weekday = ["月", "火", "水", "木", "金", "土", "日"][dt.weekday()]
    date_label = f"{dt.month}/{dt.day}({weekday})"
    date_hyphen = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    # predictions_cacheから予測を取得（あれば再計算不要）
    with get_db() as conn:
        cached_races = conn.execute("""
            SELECT ra.race_id, ra.race_name, ra.venue, ra.distance, ra.surface,
                   ra.track_condition, ra.grade, pc.predictions_json, pc.all_bets_json
            FROM races ra
            JOIN predictions_cache pc ON ra.race_id = pc.race_id
            WHERE (ra.race_date = ? OR ra.race_date = ?)
            AND ra.race_number = 11
            ORDER BY ra.venue
        """, (date_str, date_hyphen)).fetchall()

    if not cached_races:
        print(f"❌ {date_str} の予測データがありません")
        print("  先にpredict.pyで予測を実行してください")
        return

    print(f"🏇 {date_label} メインレース {len(cached_races)}件を投稿\n")

    # 各レースの◎○▲をmarkフィールドから取得
    race_data = []
    for race in cached_races:
        preds = json.loads(race['predictions_json']) if race['predictions_json'] else []
        all_bets = json.loads(race['all_bets_json']) if race['all_bets_json'] else {}

        marks = {'◎': None, '○': None, '▲': None}
        for p in preds:
            m = p.get('mark', '')
            if m in marks and marks[m] is None:
                marks[m] = p

        # markがなければpred_win順で割り当て
        if not marks['◎']:
            sorted_p = sorted(preds, key=lambda x: x.get('pred_win', 0), reverse=True)
            mark_list = ['◎', '○', '▲']
            for i, p in enumerate(sorted_p[:3]):
                if mark_list[i] not in marks or marks[mark_list[i]] is None:
                    marks[mark_list[i]] = p

        race_data.append({
            'race': race,
            'marks': marks,
            'all_bets': all_bets,
        })

    # ── ツイート1: 概要 ──
    venues = list(set(r['race']['venue'] for r in race_data))
    t1 = f"🏇 {date_label} AI予想\n"
    t1 += f"開催: {'・'.join(venues)}\n\n"

    for rd in race_data:
        race = rd['race']
        m = rd['marks']
        rname = race['race_name']
        grade = f" [{race['grade']}]" if race['grade'] else ""
        t1 += f"📍{race['venue']} {rname}{grade}\n"
        if m['◎']:
            t1 += f"  ◎{m['◎'].get('horse_name','?')}\n"

    t1 += "\n#競馬予想 #AI予想 🧵↓"

    # ── ツイート2: 各レースの詳細 ──
    t2 = ""
    for rd in race_data:
        race = rd['race']
        m = rd['marks']
        rname = race['race_name']
        t2 += f"🏟️{race['venue']} {rname}\n"

        for mark_str in ['◎', '○', '▲']:
            p = m[mark_str]
            if p:
                t2 += f"{mark_str}{p.get('horse_number',0)}{p.get('horse_name','?')} "
        t2 = t2.rstrip() + "\n\n"

    # ── ツイート3: 推奨買い目 ──
    t3 = "💡 AI推奨買い目\n\n"
    for rd in race_data:
        race = rd['race']
        all_bets = rd['all_bets']
        rname = race['race_name']

        # 各券種のベスト買い目をピックアップ（EV上位）
        best_bets = []
        for bt, bt_bets in all_bets.items():
            for b in bt_bets:
                best_bets.append({**b, 'bt': bt})

        best_bets.sort(key=lambda x: x.get('ev', 0), reverse=True)

        t3 += f"・{rname}\n"
        if best_bets:
            for b in best_bets[:2]:
                t3 += f" {b['bt']} {b.get('detail','')} (EV{b.get('ev',0):.1f})\n"
        else:
            t3 += " 見送り推奨\n"

    t3 += "\n的中結果は本日夕方に報告します📊"

    tweets = [t1, t2, t3]

    client = None
    if not args.dry_run:
        client = load_x_client()
        if not client:
            return

    post_thread(client, tweets, dry_run=args.dry_run)


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
    if roi >= 100:
        t1 += "プラス回収！📈🔥\n\n"
    else:
        t1 += "本日はマイナス。改善します 📉\n\n"
    t1 += "#競馬予想 #AI予想 #競馬結果 🧵↓"

    # ── ツイート2: 各レース結果 ──
    t2 = f"📋 各レース結果\n\n"
    for r in race_results:
        m = r['marks']
        mark_str = ""
        if m['◎']:
            mark_str += f"◎{m['◎']['horse_number']}{m['◎']['horse_name']}"
        if m['○']:
            mark_str += f" ○{m['○']['horse_number']}{m['○']['horse_name']}"

        if r['hit_bets']:
            t2 += f"✅ {r['venue']} {r['race_name']}\n"
            t2 += f" {mark_str}\n"
            for hb in r['hit_bets'][:2]:
                t2 += f" 💰{hb['type']} {hb['detail']}→{hb['payout']:,}円\n"
        else:
            t2 += f"❌ {r['venue']} {r['race_name']}\n"
            t2 += f" {mark_str}\n"
            t2 += f" 推奨{r['miss_count']}点 不的中\n"

    # ── ツイート3: 総括 ──
    t3 = "💡 振り返り\n\n"
    if roi >= 100:
        t3 += f"ROI {roi}%でプラス回収\n"
        t3 += f"的中レース: {hit_races}/{total_races}\n\n"
    else:
        t3 += f"ROI {roi}%\n"
        t3 += "外れた原因を分析し精度向上します\n\n"
    t3 += "明日もメインレースAI予想を配信\n"
    t3 += "フォロー&通知ONで見逃さない🔔"

    tweets = [t1, t2, t3]

    client = None
    if not args.dry_run:
        client = load_x_client()
        if not client:
            return

    post_thread(client, tweets, dry_run=args.dry_run)


# ─── 平日コンテンツ ───
def cmd_weekday(args):
    """平日用の自動コンテンツを生成・投稿"""
    today = now_jst()
    dow = today.weekday()  # 0=月, 4=金
    print(f"📅 JST曜日: {['月','火','水','木','金','土','日'][dow]}曜日")

    if dow == 0:
        tweet = generate_weekly_summary()
    elif dow == 1:
        tweet = generate_jockey_ranking()
    elif dow == 2:
        tweet = generate_analysis_column()
    elif dow == 3:
        tweet = generate_pickup_horse()
    elif dow == 4:
        tweet = generate_weekend_preview()
    else:
        tweet = generate_analysis_column()

    if not tweet:
        print("❌ コンテンツ生成に失敗")
        return

    client = None
    if not args.dry_run:
        client = load_x_client()
        if not client:
            return

    post_tweet(client, tweet, dry_run=args.dry_run)


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
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
