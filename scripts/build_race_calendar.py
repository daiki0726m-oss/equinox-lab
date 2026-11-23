#!/usr/bin/env python3
"""開催日カレンダーを текст サイドカーに書き出す (#158)

なぜ要るか
----------
このシステムの「レース日」判定は全て曜日ベース (cron の `6,0` と `DOW -ge 6`) で、
**祝日の3日間開催 (月曜) が構造的に見えない**。2026-09-21 (敬老の日) は
阪神10R・神戸新聞杯(G2) が組まれていたが、レース枠は DB に登録された一方で
出走馬の取得も予測も走らず、ダッシュボードは発走1時間半前まで404だった。
3日間開催は年に数回ある (成人の日/GW/敬老の日/スポーツの日/文化の日 等)。

曜日を足して回る対症療法はやめる。#2 の馬番ガードと同じ考え方で
**「DB にレースが入っているか」をデータ駆動の判定にする**。
ただし watchdog は keiba.db を持たない (正本は .gz、展開もしない #150) ので、
DB を読めるワークフロー側でこのカレンダーを書き出し、
watchdog は git 上のテキストを読むだけにする。

出力: docs/data/race_calendar.json
  {"generated_at": ..., "dates": {"2026-09-21": {"races": 10, "venues": ["阪神"]}, ...}}
  対象は「今日を含む前後14日」。

--probe (#179 Part D): カレンダーの「発見」
-------------------------------------------
時計 (raceday_clock) もセーフティネットも、土日以外の開催日が **DB (= このカレンダー) に入っていなければ
何も起きない**。fetch_weekend_races は次の土日しか取らないので、月曜祝日・順延の火曜が DB に入るのは
偶然だった (2026-11-23 は 24R 中 6R だけ載っていた)。
--probe の時だけ、netkeiba の月カレンダー (窓 [今日, 今日+14] にかかる1〜2か月) の kaisai_date のうち
**平日 (月〜金) かつ DB 由来の dates に無い日**について既存のレース一覧の取得 (scraper.get_race_list_by_date)
を呼び、JRA の場コード 01-10 のレースがあれば {"races": n, "venues": [...], "source": "netkeiba"} を足す。
  - 無ければ足さない (海外馬券・中止・未発表を「開催日」にしない)。土日は触らない。DB にある日は上書きしない。
  - 失敗・タイムアウトは DB 由来だけで書く (先に DB 由来を書いてから問い合わせるので、外側の
    `timeout 120` に殺されても DB 由来の版が残る。呼び出し側は `|| 従来の呼び出し` で必ず書き直す)。
  - DB には書かない (scraper の __init__ は init_db で keiba.db を触るので呼ばない)。通信は 1〜3 回/回
    (REQUEST_INTERVAL つき)。netkeiba の race ページは UTF-8 で、文字コードは scraper._get がサーバの
    Content-Type に従う (#85)。ここで読むのは数字 (kaisai_date / race_id) だけ。
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "data", "race_calendar.json")
WINDOW_DAYS = 14
PROBE_DEADLINE_S = 100           # 呼び出し側の timeout 120 より前に自分で打ち切る
CALENDAR_URL = "https://race.netkeiba.com/top/calendar.html?year={y}&month={m}"
VENUE_NAMES = {1: "札幌", 2: "函館", 3: "福島", 4: "新潟", 5: "東京",
               6: "中山", 7: "中京", 8: "京都", 9: "阪神", 10: "小倉"}


class ProbeError(Exception):
    pass


def db_dates(db, lo, hi):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    dates = {}
    try:
        for r in conn.execute(
            "SELECT race_date, COUNT(*) n, GROUP_CONCAT(DISTINCT venue) v FROM races "
            "WHERE race_date BETWEEN ? AND ? AND race_date != '' "
            # #161: JRA の場コード (01-10) だけ。地方競馬の残骸行 (venue 空) が1行でもあると、
            # その日が「開催日」になり非開催日に予測・投稿を dispatch しうる。
            "AND CAST(substr(race_id, 5, 2) AS INT) BETWEEN 1 AND 10 AND venue != '' "
            "GROUP BY race_date",
                (lo.isoformat(), hi.isoformat())):
            dates[r["race_date"]] = {
                "races": r["n"],
                "venues": sorted((r["v"] or "").split(",")) if r["v"] else [],
            }
    finally:
        conn.close()
    return dates


def kaisai_dates_from_html(text):
    """netkeiba の月カレンダーの HTML から kaisai_date=YYYYMMDD を拾う (scraper.get_race_list と同じ規則)。"""
    out = []
    for d8 in sorted(set(re.findall(r"kaisai_date=(\d{8})", text or ""))):
        try:
            out.append(datetime.strptime(d8, "%Y%m%d").date())
        except ValueError:
            continue
    return out


def jra_summary(race_ids):
    """レース一覧の race_id から JRA (場コード 01-10) のレース数と場名。それ以外 (海外など) は数えない。"""
    by = {}
    for rid in race_ids or []:
        rid = str(rid)
        if not re.fullmatch(r"\d{12}", rid):
            continue
        code = int(rid[4:6])
        if 1 <= code <= 10:
            by.setdefault(code, set()).add(rid)
    n = sum(len(v) for v in by.values())
    return n, sorted(VENUE_NAMES[c] for c in by)


def months_covering(lo, hi):
    out, y, m = [], lo.year, lo.month
    while (y, m) <= (hi.year, hi.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def make_scraper():
    """既存のスクレイパー (間隔制御・文字コード #85 込み) を、keiba.db に触れずに使う。"""
    import requests
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from scraper import NetkeibaScraper
    s = NetkeibaScraper.__new__(NetkeibaScraper)    # __init__ は init_db() で DB を触るので呼ばない
    s.session = requests.Session()
    s.session.headers.update(NetkeibaScraper.HEADERS)
    return s


def probe(dates, today, scraper, deadline=None, log=print):
    """平日 (月〜金) の開催日のうち dates に無いものを netkeiba で見つけて返す ({iso: info})。
    カレンダーを取れない・締切を過ぎた → ProbeError (呼び出し側は DB 由来だけで書く)。"""
    lo, hi = today, today + timedelta(days=WINDOW_DAYS)
    cands = set()
    for y, m in months_covering(lo, hi):
        if deadline is not None and time.monotonic() > deadline:
            raise ProbeError("締切を過ぎた (カレンダーの取得中)")
        resp = scraper._get(CALENDAR_URL.format(y=y, m=m))
        if not resp:
            raise ProbeError(f"netkeiba の {y}年{m}月のカレンダーを取れない")
        got = kaisai_dates_from_html(resp.text)
        if not got:
            raise ProbeError(f"{y}年{m}月のカレンダーに kaisai_date が1つも無い (ページの形式が変わった疑い)")
        for d in got:
            if lo <= d <= hi and d.weekday() < 5 and d.isoformat() not in dates:
                cands.add(d)
    add = {}
    for d in sorted(cands):
        if deadline is not None and time.monotonic() > deadline:
            raise ProbeError(f"締切を過ぎた (レース一覧 {d} の前)")
        rids = scraper.get_race_list_by_date(d.strftime("%Y%m%d"))
        n, venues = jra_summary(rids)
        if n:
            add[d.isoformat()] = {"races": n, "venues": venues, "source": "netkeiba"}
            log(f"   🔎 {d} (平日) は netkeiba に JRA の開催あり ({n}R / {'・'.join(venues)}) → カレンダーに足す")
        else:
            log(f"   🔎 {d} (平日) は netkeiba のカレンダーにあるが JRA のレースが無い "
                f"(海外・中止・未発表) → 足さない")
    return add


def write_payload(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="開催日カレンダー (docs/data/race_calendar.json) を書き出す")
    ap.add_argument("--probe", action="store_true",
                    help="netkeiba のカレンダーで平日の開催日を発見して足す (#179 Part D)")
    ap.add_argument("--db", default=os.path.join(ROOT, "keiba.db"), help=argparse.SUPPRESS)
    ap.add_argument("--out", default=OUT, help=argparse.SUPPRESS)
    ap.add_argument("--today", help=argparse.SUPPRESS)           # テスト用 YYYY-MM-DD
    a = ap.parse_args(argv)

    if not os.path.exists(a.db):
        print("⚠️ keiba.db が無いのでカレンダーを更新しません (既存値を維持)")
        return 1
    now = datetime.now(JST)
    today = date.fromisoformat(a.today) if a.today else now.date()
    lo, hi = today - timedelta(days=WINDOW_DAYS), today + timedelta(days=WINDOW_DAYS)
    dates = db_dates(a.db, lo, hi)
    payload = {"generated_at": now.isoformat(),
               "window": [lo.isoformat(), hi.isoformat()], "dates": dates}
    # 先に DB 由来を書く (この後の問い合わせが外側の timeout に殺されても、この版が残る)
    write_payload(a.out, payload)

    if a.probe:
        info = {"at": datetime.now(JST).isoformat(), "added": []}
        try:
            add = probe(dict(dates), today, make_scraper(),
                        deadline=time.monotonic() + PROBE_DEADLINE_S)
        except Exception as e:      # 失敗・締切 → DB 由来だけ (上で書き済み)
            info["status"] = f"failed: {type(e).__name__}: {e}"[:300]
            print(f"::warning::#179: netkeiba での開催日の発見に失敗 ({e}) → DB 由来の日付だけで書く")
        else:
            info["status"] = "ok"
            info["added"] = sorted(add)
            for k, v in add.items():
                if k not in dates and date.fromisoformat(k).weekday() < 5:   # 念のため二重に守る
                    dates[k] = v
            payload["dates"] = dict(sorted(dates.items()))
            print(f"🔎 #179: netkeiba で平日の開催日を確認 — 追加 {len(add)}日"
                  + (f" ({', '.join(sorted(add))})" if add else ""))
        payload["probe"] = info
        write_payload(a.out, payload)

    t = dates.get(today.isoformat())
    print(f"📅 開催日カレンダー更新: {len(dates)}日分 "
          f"({lo} 〜 {hi})")
    print(f"   本日 {today} は " +
          (f"開催あり ({t['races']}R / {'・'.join(t['venues'])})" if t else "開催なし"))
    # 曜日ベースの想定と食い違う日を目立たせる (3日間開催の検知)
    extra = [d for d in sorted(dates)
             if datetime.fromisoformat(d).weekday() < 5]
    if extra:
        print(f"   ⚠️ 平日開催 (曜日ベースの判定から漏れる日): {', '.join(extra)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
