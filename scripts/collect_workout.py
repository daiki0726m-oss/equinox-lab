#!/usr/bin/env python3
"""Track 2: 追切タイム scraper

netkeiba の追切ページ (https://race.netkeiba.com/race/oikiri.html?race_id=XXX)
から週次の追切時計を取得して DB に保存。

注意: netkeiba access が必要。実行前に既存 scraper.py で動作確認すること。

DB schema (新規 table):
    CREATE TABLE workouts (
        horse_id TEXT,
        date TEXT,         -- 追切実施日 (YYYY-MM-DD)
        course TEXT,       -- W (坂路) / D (ダート) / 美 (美南W) 等
        condition TEXT,    -- 良/稍重/重 等
        time_full TEXT,    -- "5F 67.3-52.8-38.5-25.2-12.5"
        last_1f REAL,      -- ラスト 1F タイム (秒)
        last_3f REAL,      -- ラスト 3F
        evaluation TEXT,   -- 強め追い / 馬なり / 一杯 等
        partner_horse_id TEXT,  -- 併せ馬相手
        partner_margin TEXT,    -- 併せた結果 (先着 / 同入 / 遅れ)
        scraped_at TIMESTAMP,
        PRIMARY KEY (horse_id, date, course)
    );

Usage:
    python3 scripts/collect_workout.py --race-id 202605021211
    python3 scripts/collect_workout.py --date 2026-05-31  # その日の全 race
    python3 scripts/collect_workout.py --init-db  # table 作成のみ
"""
from __future__ import annotations
import argparse
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database import get_db, init_db

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("⚠️ 必要: pip install requests beautifulsoup4")
    sys.exit(1)


WORKOUT_URL_TMPL = "https://race.netkeiba.com/race/oikiri.html?race_id={race_id}"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def init_workout_table():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workouts (
                horse_id TEXT NOT NULL,
                date TEXT NOT NULL,
                course TEXT,
                condition TEXT,
                time_full TEXT,
                last_1f REAL,
                last_3f REAL,
                evaluation TEXT,
                partner_horse_id TEXT,
                partner_margin TEXT,
                scraped_at TIMESTAMP,
                PRIMARY KEY (horse_id, date, course)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_workouts_horse ON workouts(horse_id, date DESC)
        """)
    print("✅ workouts table 初期化済")


def scrape_workouts(race_id: str):
    """1 race の追切ページから出走馬全頭の追切時計を取得"""
    url = WORKOUT_URL_TMPL.format(race_id=race_id)
    print(f"📥 GET {url}")

    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding

    soup = BeautifulSoup(resp.text, "html.parser")

    # 追切テーブルは複数あり、各馬の最新追切を抽出
    # 注: netkeiba の HTML 構造は変わるので、現時点での推定で書く
    workouts = []
    tables = soup.find_all("table", class_=re.compile("Oikiri|追切"))

    for table in tables:
        rows = table.find_all("tr")
        # ヘッダー解析
        for tr in rows[1:]:
            tds = tr.find_all("td")
            if len(tds) < 6: continue
            # 暫定: 馬名・日付・タイム
            try:
                horse_name = tds[1].get_text(strip=True) if len(tds) > 1 else ""
                date_str = tds[2].get_text(strip=True) if len(tds) > 2 else ""
                course = tds[3].get_text(strip=True) if len(tds) > 3 else ""
                cond = tds[4].get_text(strip=True) if len(tds) > 4 else ""
                time_full = tds[5].get_text(strip=True) if len(tds) > 5 else ""
                eval_str = tds[6].get_text(strip=True) if len(tds) > 6 else ""

                # last_1f を時間文字列から抽出 (例: "67.3-52.8-38.5-25.2-12.5" → 12.5)
                last_1f, last_3f = None, None
                m = re.findall(r"(\d+\.\d+)", time_full)
                if m:
                    nums = [float(x) for x in m]
                    last_1f = nums[-1] if len(nums) >= 1 else None
                    last_3f = nums[-3] if len(nums) >= 3 else None

                workouts.append({
                    "horse_name": horse_name,
                    "date": date_str,
                    "course": course,
                    "condition": cond,
                    "time_full": time_full,
                    "last_1f": last_1f,
                    "last_3f": last_3f,
                    "evaluation": eval_str,
                })
            except Exception as e:
                continue

    print(f"📊 抽出: {len(workouts)} 件")
    return workouts


def save_workouts(workouts, race_id):
    """horse_name → horse_id 解決後 DB へ保存"""
    if not workouts: return 0
    init_db()
    saved = 0
    with get_db() as conn:
        # race_id から出走馬を取得 (horse_name → horse_id map)
        rows = conn.execute("""
            SELECT res.horse_id, h.horse_name FROM results res
            LEFT JOIN horses h ON res.horse_id = h.horse_id
            WHERE res.race_id = ?
        """, (race_id,)).fetchall()
        name_to_id = {r["horse_name"]: r["horse_id"] for r in rows if r["horse_name"]}

        for w in workouts:
            hid = name_to_id.get(w["horse_name"])
            if not hid:
                continue
            conn.execute("""
                INSERT OR REPLACE INTO workouts
                (horse_id, date, course, condition, time_full, last_1f, last_3f, evaluation,
                 partner_horse_id, partner_margin, scraped_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            """, (hid, w["date"], w["course"], w["condition"], w["time_full"],
                  w["last_1f"], w["last_3f"], w["evaluation"], datetime.now().isoformat()))
            saved += 1
    return saved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init-db", action="store_true", help="workouts table 作成のみ")
    ap.add_argument("--race-id", help="特定 race の追切取得")
    ap.add_argument("--date", help="その日の全 race の追切取得 (YYYY-MM-DD)")
    args = ap.parse_args()

    init_workout_table()
    if args.init_db:
        return

    if args.race_id:
        ws = scrape_workouts(args.race_id)
        n = save_workouts(ws, args.race_id)
        print(f"✅ 保存: {n} 件")
    elif args.date:
        # date の全 race を取得
        init_db()
        with get_db() as conn:
            race_ids = [r["race_id"] for r in conn.execute(
                "SELECT race_id FROM races WHERE race_date = ?", (args.date,)
            ).fetchall()]
        print(f"📊 対象 race: {len(race_ids)}")
        for rid in race_ids:
            try:
                ws = scrape_workouts(rid)
                save_workouts(ws, rid)
                time.sleep(1.5)  # rate limit
            except Exception as e:
                print(f"⚠️ {rid}: {e}")
    else:
        print("--race-id または --date 必須")


if __name__ == "__main__":
    main()
