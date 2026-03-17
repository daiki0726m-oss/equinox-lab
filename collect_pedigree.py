"""
血統データ一括収集スクリプト v3
- /horse/ped/ ページから blood_table を解析
- リトライ + セッション再利用
"""

import time
import sys
import os
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))
from database import init_db, get_db


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
}


def scrape_horse_pedigree(session, horse_id, max_retries=3):
    """netkeibaの血統ページから父・母父を取得"""
    url = f"https://db.netkeiba.com/horse/ped/{horse_id}/"

    for attempt in range(max_retries):
        try:
            resp = session.get(url, timeout=20)
            resp.encoding = "euc-jp"
            if resp.status_code != 200:
                return None

            soup = BeautifulSoup(resp.text, "html.parser")
            bt = soup.find("table", class_="blood_table")
            if not bt:
                return None

            trs = bt.find_all("tr")
            if len(trs) < 17:
                return None

            # Row 0: 最初のtd (rowspan=16) = 父 (Sire)
            sire_td = trs[0].find("td")
            sire = ""
            if sire_td:
                a = sire_td.find("a")
                sire = a.get_text(strip=True) if a else sire_td.get_text(strip=True)

            # Row 16: rowspan=16のtd = 母(Dam), rowspan=8のtd = 母父(Damsire)
            damsire = ""
            for td in trs[16].find_all("td"):
                rs = int(td.get("rowspan", 1))
                if rs == 8:
                    a = td.find("a")
                    damsire = a.get_text(strip=True) if a else td.get_text(strip=True)
                    break

            if sire or damsire:
                return {"sire": sire, "damsire": damsire}
            return None

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt < max_retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            return None
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return None

    return None


def main():
    init_db()

    with get_db() as conn:
        horses = conn.execute("""
            SELECT horse_id, horse_name
            FROM horses
            WHERE (sire IS NULL OR sire = '')
              AND horse_id != ''
            ORDER BY horse_id
        """).fetchall()

    total = len(horses)
    if total == 0:
        print("✅ 全馬の血統データ取得済みです")
        return

    print(f"🧬 血統データ収集: {total}頭")

    session = requests.Session()
    session.headers.update(HEADERS)

    success = 0
    fail = 0
    t_start = time.time()

    for i, horse in enumerate(horses):
        horse_id = horse["horse_id"]

        if (i + 1) % 100 == 0 or i == 0:
            elapsed = time.time() - t_start
            speed = (i + 1) / elapsed if elapsed > 0 else 0
            remaining = (total - i - 1) / speed / 60 if speed > 0 else 0
            print(f"  [{i+1}/{total}] 成功:{success} 失敗:{fail} "
                  f"速度:{speed:.1f}頭/秒 残り:{remaining:.0f}分")

        pedigree = scrape_horse_pedigree(session, horse_id)

        if pedigree:
            with get_db() as conn:
                conn.execute("""
                    UPDATE horses SET sire = ?, damsire = ?
                    WHERE horse_id = ?
                """, (pedigree["sire"], pedigree["damsire"], horse_id))
            success += 1
        else:
            fail += 1

        # レート制限 (0.5秒間隔)
        time.sleep(0.5)

    elapsed = time.time() - t_start
    print(f"\n✅ 血統データ収集完了 ({elapsed/60:.1f}分)")
    print(f"  成功: {success}")
    print(f"  失敗: {fail}")


if __name__ == "__main__":
    main()
