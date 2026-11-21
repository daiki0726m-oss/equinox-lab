"""
2026年の既存レースから配当データを収集するスクリプト
既にDBにある結果レースの配当を追加取得する
"""
import sys
sys.path.insert(0, '.')
from scraper import NetkeibaScraper
from database import get_db, init_db

init_db()
scraper = NetkeibaScraper()

# 既にDBにある2026年のレースIDを取得（結果があるもの）
with get_db() as conn:
    rows = conn.execute("""
        SELECT DISTINCT r.race_id
        FROM races r
        INNER JOIN results res ON r.race_id = res.race_id
        WHERE r.race_date >= '2026-01-01' AND r.race_date <= '2026-03-31'
        AND res.finish_position > 0
        AND r.race_id NOT IN (SELECT DISTINCT race_id FROM payouts)
        ORDER BY r.race_id
    """).fetchall()
    race_ids = [row['race_id'] for row in rows]

print(f"🎯 配当未取得のレース: {len(race_ids)}件")

for i, race_id in enumerate(race_ids):
    print(f"  [{i+1}/{len(race_ids)}] {race_id}")
    race_data = scraper.scrape_race_result(race_id)
    if race_data and race_data.get("payouts"):
        with get_db() as conn:
            for p in race_data["payouts"]:
                conn.execute("""
                    INSERT OR REPLACE INTO payouts
                    (race_id, bet_type, combination, payout_amount, popularity)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    race_id, p["bet_type"],
                    p["combination"], p["payout_amount"],
                    p.get("popularity", 0)
                ))
        print(f"    💰 {len(race_data['payouts'])}件の配当保存")
    else:
        print(f"    ⚠️ 配当なし")

print("🎉 配当収集完了！")
