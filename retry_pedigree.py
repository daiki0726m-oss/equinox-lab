"""
血統データ失敗分リトライスクリプト
pedigree テーブルに未登録の horse_id を再取得
"""
import time
import sys
import os
import sqlite3

sys.path.insert(0, os.path.dirname(__file__))

from scraper import NetkeibaScraper
from database import get_db, init_db


def retry_pedigree():
    init_db()
    scraper = NetkeibaScraper()

    # pedigree テーブルに登録済みの horse_id を取得
    with get_db() as conn:
        existing = set(
            r["horse_id"] for r in
            conn.execute("SELECT DISTINCT horse_id FROM pedigree").fetchall()
        )
        all_horses = [
            r["horse_id"] for r in
            conn.execute("SELECT DISTINCT horse_id FROM horses").fetchall()
        ]

    missing = [h for h in all_horses if h not in existing]
    print(f"📊 血統データリトライ:")
    print(f"   全馬: {len(all_horses)}")
    print(f"   登録済み: {len(existing)}")
    print(f"   未登録 (リトライ対象): {len(missing)}")

    if not missing:
        print("✅ 全馬の血統データが登録済みです!")
        return

    success = 0
    fail = 0

    for i, horse_id in enumerate(missing):
        try:
            url = f"https://db.netkeiba.com/horse/{horse_id}/"
            r = scraper._get(url)
            if not r:
                fail += 1
                continue

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, "html.parser")

            # 血統テーブル
            pedigree_table = soup.select_one("table.blood_table")
            if not pedigree_table:
                fail += 1
                continue

            rows = pedigree_table.select("td a")
            ancestors = []
            for a in rows:
                name = a.text.strip()
                href = a.get("href", "")
                ancestor_id = ""
                if "/horse/" in href:
                    ancestor_id = href.split("/horse/")[-1].rstrip("/")
                ancestors.append({"name": name, "id": ancestor_id})

            # 父, 母, 父父, 父母, 母父, 母母 の順
            labels = ["sire", "dam", "sire_sire", "sire_dam", "dam_sire", "dam_dam"]
            pedigree_data = {}
            for j, label in enumerate(labels):
                if j < len(ancestors):
                    pedigree_data[f"{label}_id"] = ancestors[j]["id"]
                    pedigree_data[f"{label}_name"] = ancestors[j]["name"]
                else:
                    pedigree_data[f"{label}_id"] = ""
                    pedigree_data[f"{label}_name"] = ""

            with get_db() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO pedigree
                    (horse_id, sire_id, sire_name, dam_id, dam_name,
                     sire_sire_id, sire_sire_name, sire_dam_id, sire_dam_name,
                     dam_sire_id, dam_sire_name, dam_dam_id, dam_dam_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    horse_id,
                    pedigree_data["sire_id"], pedigree_data["sire_name"],
                    pedigree_data["dam_id"], pedigree_data["dam_name"],
                    pedigree_data["sire_sire_id"], pedigree_data["sire_sire_name"],
                    pedigree_data["sire_dam_id"], pedigree_data["sire_dam_name"],
                    pedigree_data["dam_sire_id"], pedigree_data["dam_sire_name"],
                    pedigree_data["dam_dam_id"], pedigree_data["dam_dam_name"],
                ))
                conn.commit()
            success += 1

        except Exception as e:
            fail += 1

        if (i + 1) % 100 == 0:
            print(f"  [{i+1}/{len(missing)}] 成功:{success} 失敗:{fail}")

        time.sleep(1)

    print(f"\n✅ リトライ完了:")
    print(f"   成功: {success}")
    print(f"   失敗: {fail}")
    print(f"   合計登録済み: {len(existing) + success}/{len(all_horses)}")


if __name__ == "__main__":
    retry_pedigree()
