#!/usr/bin/env python3
"""天候・馬場の読み取りと保存規則の単体テスト (#169)。

  python3 scripts/test_race_meta.py        (失敗があれば exit 1)

netkeiba の RaceData01 は 2026-10-25〜26 に書式が変わった (天候はアイコンだけ、馬場は
span.Item03(芝)/Item04(ダ) の1文字)。下の HTML は 2026-11-05 に取得した実ページの
RaceData01 部分 (新書式) と、旧書式 (見出し語つき) の再現。新旧どちらも
DB の正規表記 (良/稍/重/不) で読めること、空の値で既存の値を上書きしないことを確かめる。
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup  # noqa: E402
from race_meta import (parse_weather_track, normalize_track_condition,  # noqa: E402
                       normalize_weather, keep_existing_race_meta, meta_fill_rates,
                       low_meta_fill_dates, pick_meta_refetch_dates)

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}: got {got!r} want {want!r}")


def rd01(html):
    return BeautifulSoup(f'<div class="RaceData01">{html}</div>', "lxml").find(
        "div", class_="RaceData01")


# ── 新書式 (2026-10-26 以降、実ページ) ──
NEW_DIRT = """10:15発走 /<!-- <span class="Turf"> --><span> ダ1800m</span> 右
/ <span aria-label="曇" class="Icon_Weather02 Icon_W02" role="img" title="曇"></span>
<span class="Item04"> 稍</span>
<span class="Item05">/ 直線：無風</span>"""                       # 202608041102 11/01 京都2R
NEW_TURF = """10:35発走 /<!-- <span class="Turf"> --><span> 芝1600m</span> 右 B
/ <span aria-label="晴" class="Icon_Weather02 Icon_W01" role="img" title="晴"></span>
<span class="Item03"> 良</span>
<span class="Item05">/ 直線：弱追風</span>"""                     # 202608041002 10/31 京都2R
NEW_JUMP = """11:35発走 /<!-- <span class="Turf"> --><span> 障2910m</span> 芝 ダート
/ <span aria-label="晴" class="Icon_Weather02 Icon_W01" role="img" title="晴"></span>
<span class="Item03"> 良</span>
<span class="Item04"> 重</span>
<span class="Item05">/ 直線：弱追風</span>"""                     # 202608041004 10/31 京都4R (障害)
NEW_SHUTUBA_BEFORE = """15:35発走 /<!-- <span class="Turf"> --><span> 芝1400m</span> 左 B
"""                                                               # 202605050111 発表前の出馬表
NEW_DIRT_FU = """10:05発走 /<span> ダ1200m</span> 右
/ <span class="Icon_Weather03 Icon_W03" title="雨"></span>
<span class="Item04"> 不</span>"""

check("新・ダート", parse_weather_track(rd01(NEW_DIRT)), ("曇", "稍"))
check("新・芝", parse_weather_track(rd01(NEW_TURF)), ("晴", "良"))
# 障害は芝の値 (DB の過去分の慣習: 芝ダで違う日の障害は 85/100 が芝と一致)
check("新・障害は芝の馬場", parse_weather_track(rd01(NEW_JUMP)), ("晴", "良"))
check("新・発表前の出馬表", parse_weather_track(rd01(NEW_SHUTUBA_BEFORE)), ("", ""))
check("新・不 (1文字)", parse_weather_track(rd01(NEW_DIRT_FU)), ("雨", "不"))
# aria-label だけでも読める
check("新・aria-label", parse_weather_track(rd01(
    '<span>芝1200m</span> / <span class="Icon_Weather02" aria-label="小雨"></span>'
    '<span class="Item03"> 重</span>')), ("小雨", "重"))
# 片方しか無い時はそれを使う (ダートなのに Item03 だけ、等)
check("新・芝ダの片方だけ", parse_weather_track(rd01(
    '<span> ダ1400m</span> / <span class="Item03"> 稍</span>')), ("", "稍"))

# ── 旧書式 (2026-10-25 以前。見出し語つき) ──
OLD = """15:40発走 /<!-- <span class="Turf"> --><span> 芝2200m</span> (右 A)
/ 天候:晴<span class="Icon_Weather Weather01"></span>
<span class="Item04">/ 馬場:稍</span>"""
check("旧・1文字", parse_weather_track(rd01(OLD)), ("晴", "稍"))
check("旧・2文字の不良", parse_weather_track(rd01(OLD.replace("馬場:稍", "馬場:不良"))), ("晴", "不"))
check("旧・稍重", parse_weather_track(rd01(OLD.replace("馬場:稍", "馬場:稍重"))), ("晴", "稍"))
check("旧・全角コロン", parse_weather_track(rd01(OLD.replace("天候:", "天候：").replace("馬場:", "馬場："))),
      ("晴", "稍"))
# 見出し語が無く span も無い時の最後の手段 (旧実装と同じ部分一致)
check("部分一致・重", parse_weather_track(rd01("<span> ダ1200m</span> 右 / 重")), ("", "重"))
check("RaceData01 無し", parse_weather_track(None), ("", ""))

# ── 正規化 ──
for raw, want in (("良", "良"), (" 稍", "稍"), ("稍重", "稍"), ("重", "重"), ("不", "不"),
                  ("不良", "不"), ("馬場:不良", "不"), ("ダ:重", "重"), ("/ 馬場:良", "良"),
                  ("", ""), (None, ""), ("ｘ", "")):
    check(f"normalize_track_condition({raw!r})", normalize_track_condition(raw), want)
for raw, want in (("晴", "晴"), (" 曇 ", "曇"), ("天候:小雨", "小雨"), ("", ""), (None, "")):
    check(f"normalize_weather({raw!r})", normalize_weather(raw), want)

# ── 空の値で既存の値を上書きしない ──
c = sqlite3.connect(":memory:")
c.execute("CREATE TABLE races (race_id TEXT PRIMARY KEY, race_date TEXT, weather TEXT, "
          "track_condition TEXT)")
c.execute("CREATE TABLE results (race_id TEXT, finish_position INT)")
c.execute("INSERT INTO races VALUES ('202608040812', '2026-10-24', '曇', '不')")
c.execute("INSERT INTO races VALUES ('202608040801', '2026-10-24', '', '')")
d = {"race_id": "202608040812", "weather": "", "track_condition": "", "race_name": "x"}
out = keep_existing_race_meta("202608040812", d, conn=c)
check("空なら既存を引き継ぐ", (out["weather"], out["track_condition"], out["race_name"]),
      ("曇", "不", "x"))
check("元の dict は変えない", (d["weather"], d["track_condition"]), ("", ""))
out = keep_existing_race_meta("202608040812", {"weather": "晴", "track_condition": ""}, conn=c)
check("片方だけ空", (out["weather"], out["track_condition"]), ("晴", "不"))
out = keep_existing_race_meta("202608040812", {"weather": "雨", "track_condition": "重"}, conn=c)
check("新しい値が非空なら上書きしてよい", (out["weather"], out["track_condition"]), ("雨", "重"))
out = keep_existing_race_meta("202608049999", {"weather": "", "track_condition": ""}, conn=c)
check("既存行が無い", (out["weather"], out["track_condition"]), ("", ""))
out = keep_existing_race_meta("202608040801", {"weather": "", "track_condition": ""}, conn=c)
check("既存も空", (out["weather"], out["track_condition"]), ("", ""))

# ── 充足率 (確定レース = 1着がある JRA のレースだけが母数) ──
for rid, dte, w, t in (("202608040901", "2026-10-25", "", "稍"), ("202608040902", "2026-10-25", "", ""),
                       ("202605040901", "2026-10-25", "晴", "良"), ("202608041001", "2026-10-31", "晴", "良"),
                       ("202608041002", "2026-10-31", "", "")):      # 最後は未走 (着順なし)
    c.execute("INSERT INTO races VALUES (?,?,?,?)", (rid, dte, w, t))
for rid in ("202608040901", "202608040902", "202605040901", "202608041001", "202608040812"):
    c.execute("INSERT INTO results VALUES (?, 1)", (rid,))
rates = {r["race_date"]: (r["n"], round(r["weather"], 3), round(r["track"], 3))
         for r in meta_fill_rates(c, "2026-10-20", "2026-11-05")}
check("充足率 10/24", rates.get("2026-10-24"), (1, 1.0, 1.0))
check("充足率 10/25", rates.get("2026-10-25"), (3, 0.333, 0.667))
check("充足率 10/31 (未走は数えない)", rates.get("2026-10-31"), (1, 1.0, 1.0))
check("90%未満の開催日", [r["race_date"] for r in low_meta_fill_dates(c, "2026-10-20", "2026-11-05")],
      ["2026-10-25"])

# ── scraper._parse_race_info (本番の経路) も新書式で埋まる ──
from scraper import NetkeibaScraper  # noqa: E402
page = BeautifulSoup(
    '<html><head><title>２歳未勝利 結果・払戻 | 2026年11月1日 京都2R レース情報(JRA) - netkeiba'
    f'</title></head><body><div class="RaceData01">{NEW_DIRT}</div></body></html>', "lxml")
info = NetkeibaScraper._parse_race_info(None, page, "202608041102")
check("scraper 新書式", (info["weather"], info["track_condition"], info["surface"], info["race_date"]),
      ("曇", "稍", "ダート", "2026-11-01"))

# ── 自己修復で取り直す開催日の選び方 (輪番、#169 レビュー) ──
import datetime as _dt  # noqa: E402
_jst = _dt.timezone(_dt.timedelta(hours=9))
_low = [{"race_date": d} for d in ("2026-10-24", "2026-10-25", "2026-10-31", "2026-11-01")]
_sat20 = pick_meta_refetch_dates(_low, now=_dt.datetime(2026, 11, 7, 20, 5, tzinfo=_jst))
_sat22 = pick_meta_refetch_dates(_low, now=_dt.datetime(2026, 11, 7, 22, 5, tzinfo=_jst))
check("土曜 20時と22時の2回で4日すべてを取り直す", sorted(_sat20 + _sat22),
      ["2026-10-24", "2026-10-25", "2026-10-31", "2026-11-01"])
check("1回は最大2日", (len(_sat20), len(_sat22)), (2, 2))
check("着順の取り直しと重複しない", "2026-11-01" in pick_meta_refetch_dates(
    _low, exclude=["2026-11-01"], now=_dt.datetime(2026, 11, 8, 20, 5, tzinfo=_jst)), False)
check("2日以下ならそのまま (新しい順)", pick_meta_refetch_dates(_low[:2]), ["2026-10-25", "2026-10-24"])
# 埋まらない日が2日あっても、残りの日が毎回後回しにはならない (旧: 新しい順の先頭2日に固定)
_seen = set()
for _h in range(0, 24, 2):
    _seen.update(pick_meta_refetch_dates(_low, now=_dt.datetime(2026, 11, 9, _h, 0, tzinfo=_jst)))
check("輪番で全日に順番が回る", sorted(_seen), ["2026-10-24", "2026-10-25", "2026-10-31", "2026-11-01"])

# ── commit 直前の巻き戻し阻止: 空の天候・馬場だけを origin の値で埋める (db_pack のガード) ──
import tempfile  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_guard_future_races import fill_race_meta  # noqa: E402
with tempfile.TemporaryDirectory() as _td:
    _lp, _op = os.path.join(_td, "local.db"), os.path.join(_td, "origin.db")
    for _p, _rows in ((_lp, [("A", "", ""), ("B", "晴", ""), ("C", "雨", "重"), ("D", "", "")]),
                      (_op, [("A", "曇", "稍"), ("B", "雨", "不"), ("C", "晴", "良"), ("D", "", "")])):
        _c = sqlite3.connect(_p)
        _c.execute("CREATE TABLE races (race_id TEXT PRIMARY KEY, weather TEXT, track_condition TEXT)")
        _c.executemany("INSERT INTO races VALUES (?,?,?)", _rows)
        _c.commit()
        _c.close()
    _n = fill_race_meta(_lp, _op)
    _c = sqlite3.connect(_lp)
    _got = {r[0]: (r[1], r[2]) for r in _c.execute("SELECT * FROM races")}
    _c.close()
    check("空は origin で埋める", _got["A"], ("曇", "稍"))
    check("非空の local は変えない (片方だけ空)", _got["B"], ("晴", "不"))
    check("非空の local は変えない", _got["C"], ("雨", "重"))
    check("origin も空なら空のまま", _got["D"], ("", ""))
    check("埋めた件数", _n, {"weather": 1, "track_condition": 2})

if FAILS:
    print(f"❌ {len(FAILS)} 件失敗")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("✅ race_meta: 全テスト通過")
