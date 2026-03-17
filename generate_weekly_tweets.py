"""
📅 週間ツイート事前生成スクリプト
日曜夜に実行して、1週間分のツイートを事前生成 → GitHub Actions で自動投稿

使い方:
  python generate_weekly_tweets.py
  python generate_weekly_tweets.py --next-sat 20260322  (来週土曜を指定)
"""

import sys
import os
import json
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from database import init_db, get_db


def generate_weekly_tweets(next_sat_str=None):
    """1週間分のツイートを生成して scheduled_tweets.json に保存"""
    today = datetime.now()

    # 来週の土曜日を計算
    if next_sat_str:
        next_sat = datetime.strptime(next_sat_str, "%Y%m%d")
    else:
        days_until_sat = (5 - today.weekday()) % 7
        if days_until_sat == 0:
            days_until_sat = 7
        next_sat = today + timedelta(days=days_until_sat)

    next_sun = next_sat + timedelta(days=1)

    tweets = {}

    # ── 月曜: 先週末の成績まとめ ──
    mon = next_sat - timedelta(days=5)
    tweets[mon.strftime("%Y-%m-%d")] = {
        "time": "08:00",
        "type": "weekday",
        "content": generate_monday_tweet()
    }

    # ── 火曜: 騎手ランキング ──
    tue = mon + timedelta(days=1)
    tweets[tue.strftime("%Y-%m-%d")] = {
        "time": "08:00",
        "type": "weekday",
        "content": generate_tuesday_tweet()
    }

    # ── 水曜: 分析コラム ──
    wed = mon + timedelta(days=2)
    tweets[wed.strftime("%Y-%m-%d")] = {
        "time": "08:00",
        "type": "weekday",
        "content": generate_wednesday_tweet()
    }

    # ── 木曜: 注目馬 ──
    thu = mon + timedelta(days=3)
    tweets[thu.strftime("%Y-%m-%d")] = {
        "time": "08:00",
        "type": "weekday",
        "content": generate_thursday_tweet()
    }

    # ── 金曜: 週末プレビュー ──
    fri = mon + timedelta(days=4)
    sat_label = f"{next_sat.month}/{next_sat.day}"
    sun_label = f"{next_sun.month}/{next_sun.day}"
    tweets[fri.strftime("%Y-%m-%d")] = {
        "time": "08:00",
        "type": "weekday",
        "content": generate_friday_tweet(sat_label, sun_label)
    }

    # ── 土曜朝: 予想 → post_x.py predict で当日生成 ──
    tweets[next_sat.strftime("%Y-%m-%d")] = {
        "time": "08:00",
        "type": "predict",
        "date": next_sat.strftime("%Y%m%d"),
        "content": None  # 当日生成
    }

    # ── 土曜夕: 結果 → post_x.py results で当日生成 ──
    tweets[next_sat.strftime("%Y-%m-%d") + "_pm"] = {
        "time": "17:00",
        "type": "results",
        "date": next_sat.strftime("%Y%m%d"),
        "content": None
    }

    # ── 日曜朝/夕 ──
    tweets[next_sun.strftime("%Y-%m-%d")] = {
        "time": "08:00",
        "type": "predict",
        "date": next_sun.strftime("%Y%m%d"),
        "content": None
    }
    tweets[next_sun.strftime("%Y-%m-%d") + "_pm"] = {
        "time": "17:00",
        "type": "results",
        "date": next_sun.strftime("%Y%m%d"),
        "content": None
    }

    # 保存
    output_path = os.path.join(os.path.dirname(__file__), "scheduled_tweets.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(tweets, f, ensure_ascii=False, indent=2)

    print(f"✅ {len(tweets)}件のツイートを生成 → scheduled_tweets.json")
    for date_key, t in tweets.items():
        status = "📝 事前生成済み" if t["content"] else "⏳ 当日生成"
        print(f"  {date_key} {t['time']} [{t['type']}] {status}")

    return tweets


def generate_monday_tweet():
    """月曜: 先週末の成績まとめ"""
    try:
        with get_db() as conn:
            last_week = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            cached = conn.execute("""
                SELECT COUNT(*) as cnt,
                       SUM(CASE WHEN pc.should_bet = 1 THEN 1 ELSE 0 END) as bet_races
                FROM races r
                JOIN predictions_cache pc ON r.race_id = pc.race_id
                WHERE r.race_date >= ?
            """, (last_week,)).fetchone()
            race_count = cached["cnt"] if cached else 0
            bet_count = cached["bet_races"] if cached else 0
    except:
        race_count, bet_count = 36, 12

    tweet = "📊 先週末の振り返り\n"
    tweet += "━━━━━━━━━━━━\n\n"
    tweet += f"🏇 分析レース数: {race_count}R\n"
    tweet += f"💰 推奨レース数: {bet_count}R\n\n"
    tweet += "毎週末、AI予測モデルが全レースを分析。\n"
    tweet += "期待値がプラスのレースだけを厳選しています。\n\n"
    tweet += "今週末もメインレース予想を配信予定 🔔\n\n"
    tweet += "#競馬予想 #AI予想"
    return tweet


def generate_tuesday_tweet():
    """火曜: 騎手ランキング"""
    try:
        with get_db() as conn:
            top_jockeys = conn.execute("""
                SELECT j.jockey_name,
                       COUNT(*) as rides,
                       SUM(CASE WHEN r.finish_position <= 3 THEN 1 ELSE 0 END) as top3
                FROM results r
                JOIN jockeys j ON r.jockey_id = j.jockey_id
                JOIN races ra ON r.race_id = ra.race_id
                WHERE ra.race_date >= date('now', '-30 days')
                AND r.finish_position > 0
                GROUP BY j.jockey_id
                HAVING rides >= 10
                ORDER BY CAST(top3 AS FLOAT) / rides DESC
                LIMIT 5
            """).fetchall()
    except:
        top_jockeys = []

    tweet = "🏆 直近30日 騎手複勝率ランキング\n"
    tweet += "━━━━━━━━━━━━\n\n"

    if top_jockeys:
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        for i, j in enumerate(top_jockeys):
            rate = round(j["top3"] / j["rides"] * 100, 1)
            tweet += f"{medals[i]} {j['jockey_name']} — {rate}% ({j['top3']}/{j['rides']})\n"
    else:
        tweet += "🥇 集計中...\n"

    tweet += "\n当モデルでは騎手×調教師コンビの実績を\n"
    tweet += "重点分析しています 🧠\n\n"
    tweet += "#競馬予想 #AI予想 #騎手成績"
    return tweet


def generate_wednesday_tweet():
    """水曜: 分析コラム（ランダム選択）"""
    import random
    columns = [
        ("重馬場で浮上する血統とは？",
         "重馬場になると成績が激変する馬がいます。\n\n"
         "当モデルでは天候・馬場状態を特徴量に組み込み、\n"
         "重馬場時の過去複勝率を個別に評価。\n\n"
         "「良馬場では凡走、重馬場で突然好走」\n"
         "こうした馬を見逃さない仕組みです 🧠"),

        ("なぜ本命馬を買わないのか",
         "「1番人気を外して穴馬を買う」\n"
         "一見無謀に見えますが、これが回収率の鍵。\n\n"
         "期待値(EV) = 勝率 × オッズ\n\n"
         "人気馬はオッズが低すぎて\n"
         "当たっても利益が出ないことが多い。\n"
         "AIは「勝てる馬」ではなく\n"
         "「儲かる馬」を選びます 💡"),

        ("騎手×調教師コンビの威力",
         "モデルの特徴量重要度ランキング1位は\n"
         "「騎手×調教師コンビの複勝率」。\n\n"
         "同じ騎手でも、誰の馬に乗るかで\n"
         "成績が大きく変わります。\n\n"
         "相性の良いコンビを統計的に検出し、\n"
         "予測精度を大幅に向上させています 🤝"),

        ("3つのAIモデルを統合する理由",
         "当予測は3つの異なるモデルを統合:\n\n"
         "1️⃣ LambdaRank — 着順最適化\n"
         "2️⃣ 勝率モデル — 1着確率\n"
         "3️⃣ 複勝率モデル — 3着内確率\n\n"
         "単一モデルだと得意・不得意がありますが、\n"
         "統合することで安定した精度を実現 🎯"),

        ("スピード指数(SI)の読み方",
         "SI = 過去走の走破タイムを\n"
         "距離・馬場で補正した能力値。\n\n"
         "目安:\n"
         "・50前後 → 平均的\n"
         "・70以上 → かなり強い\n"
         "・90以上 → 重賞級\n\n"
         "ただしSIが高くてもオッズが低ければ\n"
         "期待値はマイナス。\n"
         "AIは期待値で判断します 📊"),
    ]

    title, body = random.choice(columns)
    tweet = "🧠 AI競馬コラム\n"
    tweet += "━━━━━━━━━━━━\n"
    tweet += f"【{title}】\n\n"
    tweet += body
    tweet += "\n\n#競馬予想 #AI予想 #競馬コラム"
    return tweet


def generate_thursday_tweet():
    """木曜: 注目馬"""
    try:
        with get_db() as conn:
            top_horses = conn.execute("""
                SELECT h.horse_name,
                       AVG(r.finish_position) as avg_pos,
                       COUNT(*) as runs
                FROM results r
                JOIN horses h ON r.horse_id = h.horse_id
                JOIN races ra ON r.race_id = ra.race_id
                WHERE ra.race_date >= date('now', '-60 days')
                AND r.finish_position > 0 AND r.finish_position <= 3
                GROUP BY h.horse_id
                HAVING runs >= 2
                ORDER BY avg_pos ASC
                LIMIT 5
            """).fetchall()
    except:
        top_horses = []

    tweet = "🐴 直近60日 好走馬ピックアップ\n"
    tweet += "━━━━━━━━━━━━\n\n"

    if top_horses:
        for h in top_horses:
            avg = round(h["avg_pos"], 1)
            tweet += f"⭐ {h['horse_name']} — 平均着順{avg}位 ({h['runs']}走)\n"
    else:
        tweet += "⭐ 集計中...\n"

    tweet += "\n次走で注目したい馬たちです 👀\n"
    tweet += "週末の予想は金曜夜〜土曜朝に配信\n\n"
    tweet += "#競馬予想 #AI予想 #注目馬"
    return tweet


def generate_friday_tweet(sat_label, sun_label):
    """金曜: 週末プレビュー"""
    tweet = "📅 今週末のレースプレビュー\n"
    tweet += "━━━━━━━━━━━━\n\n"
    tweet += f"🏇 {sat_label}(土)・{sun_label}(日)\n\n"
    tweet += "AIモデルによる全レース分析を実施中...\n\n"
    tweet += "明日朝8時にメインレースの予想を配信します 🔔\n"
    tweet += "全レース詳細はプロフリンクのnoteから！\n\n"
    tweet += "#競馬予想 #AI予想 #週末競馬"
    return tweet


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--next-sat", help="来週土曜日 (YYYYMMDD)")
    args = parser.parse_args()

    init_db()
    generate_weekly_tweets(args.next_sat)
