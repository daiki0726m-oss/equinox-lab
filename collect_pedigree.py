"""
血統データ一括収集スクリプト v3
- /horse/ped/ ページから blood_table を解析
- リトライ + セッション再利用
"""

import re
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


_HORSE_ID_RE = re.compile(r"/horse/(?:ped/|sire/|mare/)?([0-9A-Za-z]{10})(?:/|$)")


def _cell_name_and_id(td):
    """血統表のセルから (名前, netkeiba の馬ID) を取る。ID はセル先頭のリンク先
    (例 https://db.netkeiba.com/horse/000a0120cf/)。取れなければ ""。"""
    a = td.find("a")
    name = a.get_text(strip=True) if a else td.get_text(strip=True)
    hid = ""
    if a and a.get("href"):
        m = _HORSE_ID_RE.search(a["href"])
        if m:
            hid = m.group(1)
    return name, hid


def scrape_horse_pedigree(session, horse_id, max_retries=3):
    """netkeibaの血統ページから父・母・母父を取得
    → {"sire", "dam", "damsire", "sire_id", "dam_id", "damsire_id"}

    *_id は各セルのリンク先の netkeiba 馬ID。名前は表記が時期で揺れる
    (「ナダル」/「ナダルNadal(米)」、「Curlin(米)」/「Curlin\xa0(米)」) ので、
    同じ馬を一意に指すにはこちらを使う (母の産駒成績などを集計する時)。
    horses にまだ ID の列は無い — 列を足せば save_pedigree が自動で埋める。
    """
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
            sire = sire_id = ""
            if sire_td:
                sire, sire_id = _cell_name_and_id(sire_td)

            # Row 16: rowspan=16のtd = 母(Dam), rowspan=8のtd = 母父(Damsire)
            # #127 (2026-08-24): 旧実装は rowspan==8 (母父) を見つけた時点で break しており
            # **同じ行にある rowspan==16 の母を一度も読んでいなかった** → auto_pedigree の
            # p.get('dam','') が常に空文字を書き、horses.dam が 38,533頭すべて空だった。
            # 母系 (半兄弟の成績) を特徴量にできない構造的欠落の原因。#14/#95 と同型の silent .get()。
            dam = damsire = dam_id = damsire_id = ""
            for td in trs[16].find_all("td"):
                rs = int(td.get("rowspan", 1))
                text, hid = _cell_name_and_id(td)
                if rs == 16 and not dam:
                    dam, dam_id = text, hid
                elif rs == 8 and not damsire:
                    damsire, damsire_id = text, hid
                if dam and damsire:
                    break

            if sire or damsire or dam:
                return {"sire": sire, "dam": dam, "damsire": damsire,
                        "sire_id": sire_id, "dam_id": dam_id, "damsire_id": damsire_id}
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


PEDIGREE_FIELDS = ("sire", "dam", "damsire")
# 血統表の各馬の netkeiba 馬ID。horses に同名の列があれば save_pedigree が埋める
# (2026-10 時点では列が無い = 書かない。列の追加はスキーマ変更として別に判断する)。
PEDIGREE_ID_FIELDS = ("sire_id", "dam_id", "damsire_id")


def _pedigree_columns(conn):
    """horses に存在する血統の列 (父・母・母父 + あれば各ID)。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(horses)").fetchall()}
    return [k for k in PEDIGREE_FIELDS + PEDIGREE_ID_FIELDS if k in cols]


def save_pedigree(conn, horse_id, p, overwrite=False):
    """scrape_horse_pedigree の結果を horses に書く (父・母・母父の3列すべて)。

    既定 (overwrite=False) は **DB 側が空の列だけ** 埋め、入っている値は一切変えない。
      - 父・母父の文字列は sire_course_top3_rate などの集計単位で、DB には時期によって
        「ナダル」と「ナダルNadal(米)」のような別表記が混在している。補完のついでに
        今の血統ページの表記で書き換えると、どの処理が先に走ったかで集計単位が変わる
        (旧 auto_pedigree は上書きしていた)。表記を揃えるなら正規化として別に行う。
      - 取得できなかった項目 (空文字) で既存値を消さない (#127)。
    overwrite=True は「取れた値で既存値も置き換える」(表記の一本化など、意図して書き換える時だけ)。
    旧実装は呼び出し元ごとに UPDATE を手書きしており、母 (dam) を書き忘れる経路があった
    (collect_pedigree.main / fetch_weekend_races.yml は sire・damsire だけ書いていた)。
    Returns: 更新された行数 (0 or 1)。既定では「実際に埋めた列がある時だけ 1」= 2回目は 0 (冪等)。
    """
    if not p:
        return 0
    fields = _pedigree_columns(conn)
    vals = [str(p.get(k) or "").strip() for k in fields]
    if not any(vals):
        return 0
    if overwrite:
        sets = ", ".join(f"{k} = CASE WHEN ? <> '' THEN ? ELSE {k} END" for k in fields)
    else:
        sets = ", ".join(
            f"{k} = CASE WHEN COALESCE({k}, '') = '' AND ? <> '' THEN ? ELSE {k} END"
            for k in fields)
    params = []
    for v in vals:
        params += [v, v]
    where = "horse_id = ?"
    wparams = [horse_id]
    if not overwrite:
        # 実際に埋まる列がある行だけ更新 → rowcount が「埋めた頭数」になる (2回目は 0 = 冪等の確認に使える)
        where += " AND (" + " OR ".join(
            f"(COALESCE({k}, '') = '' AND ? <> '')" for k in fields) + ")"
        wparams += vals
    cur = conn.execute(f"UPDATE horses SET {sets} WHERE {where}", (*params, *wparams))
    return cur.rowcount


def main():
    init_db()

    with get_db() as conn:
        horses = conn.execute("""
            SELECT horse_id, horse_name
            FROM horses
            WHERE (sire IS NULL OR sire = '' OR dam IS NULL OR dam = ''
                   OR damsire IS NULL OR damsire = '')
              AND horse_id != ''
            ORDER BY horse_id DESC
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
                save_pedigree(conn, horse_id, pedigree)   # 母 (dam) も書く。既存値は上書きしない
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
