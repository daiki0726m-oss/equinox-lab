#!/usr/bin/env python3
"""着差の読み方・格・近5走の連続項目の単体テスト。

  python3 scripts/test_margin_parse.py        (失敗があれば exit 1)

対象は ml/features.py の共通関数 (学習側 fast_train.py も同じ関数を呼ぶ):
  parse_margin / margin_stats / class_level / recent_form_features
表記は DB (results.margin) に実在する全29種を網羅している。
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml.features import (parse_margin, margin_stats, class_level,  # noqa: E402
                         recent_form_features, winner_times, FeatureBuilder)

FAILS = []


def check(name, got, want, tol=1e-9):
    ok = (got is None and want is None) or (
        got is not None and want is not None and
        ((isinstance(want, float) and math.isnan(want) and math.isnan(got)) or
         abs(float(got) - float(want)) <= tol))
    if not ok:
        FAILS.append(f"{name}: got {got!r} want {want!r}")


# ── parse_margin: DB の実表記すべて ──
MARGINS = {
    "クビ": 0.15, "ハナ": 0.02, "アタマ": 0.05,
    "1/2": 0.5, "3/4": 0.75,
    "1.1/4": 1.25, "1.1/2": 1.5, "1.3/4": 1.75, "2.1/2": 2.5, "3.1/2": 3.5,
    "1": 1.0, "2": 2.0, "3": 3.0, "4": 4.0, "5": 5.0, "6": 6.0, "7": 7.0,
    "8": 8.0, "9": 9.0, "10": 10.0,
    "大": 10.0, "大差": 10.0, "同着": 0.0,
    "クビ+1/2": 0.65, "3+ハナ": 3.02, "1/2+1/2": 1.0,
    "1位降着": None, "2位降着": None,
    "": None, None: None,
    # pandas が NULL を NaN (float) で渡してくる経路 (学習側 to_dict("records"))。
    # 旧学習側は str(nan)='nan' → float('nan') を着差として採用し、margin_avg が NaN になっていた
    float("nan"): None, "nan": None,
    # 空白区切りの帯分数・全角・前後空白 (将来の表記ゆれ)
    "1 1/4": 1.25, "１.１/２": 1.5, " 3/4 ": 0.75,
    # 壊れた表記は None (例外にしない)
    "1/0": None, "abc": None, "クビ+": None, "+": None,
}
for s, want in MARGINS.items():
    check(f"parse_margin({s!r})", parse_margin(s), want)
    # 推論側の旧 API (静的メソッド) も同じ値を返す
    check(f"FeatureBuilder._parse_margin({s!r})", FeatureBuilder._parse_margin(s), want)

# ── margin_stats: 1着は着差0、読めない着差は飛ばす、上限10 ──
check("全部1着 avg", margin_stats([(1, ""), (1, None), (1, "")])[0], 0.0)
check("全部1着 best", margin_stats([(1, ""), (1, None), (1, "")])[1], 0.0)
a, b = margin_stats([(1, ""), (3, "1.1/4"), (5, "大"), (2, "1位降着")])
check("混在 avg", a, (0.0 + 1.25 + 10.0) / 3)
check("混在 best", b, 0.0)
check("データなし avg", margin_stats([])[0], 5.0)
check("読めない着差だけ", margin_stats([(4, "1位降着")])[1], 5.0)
check("同着の1着", margin_stats([(1, "同着")])[0], 0.0)

# ── class_level ──
for (g, n), want in {
    ("G1", "日本ダービー"): 7, ("G2", "札幌記念"): 6, ("G3", "新潟記念"): 5,
    ("OP", "藤森S"): 4, ("L", "x"): 4,
    ("３勝クラス", "豊前S"): 3, ("２勝クラス", "東雲賞"): 2, ("１勝クラス", "北斗特別"): 1,
    ("3勝クラス", "x"): 3,
    ("未勝利", "3歳未勝利"): 0, ("新馬", "2歳新馬"): 0, ("", "2歳新馬"): 0,
    ("OP", "4歳以上障害OP"): None, ("OP", "中山グランドJ"): None,
    ("OP", "新潟JS"): None, ("未勝利", "障害3歳以上未勝利"): None,
    ("", ""): None, (None, None): None, (float("nan"), float("nan")): None,
    # grade が空の時は race_name から読む (DB の grade 空の行に実在する表記)
    ("", "天皇賞(秋)(GI)"): 7, ("", "産経賞オールカマー(GII)"): 6, ("", "チャレンジC(GIII)"): 5,
    ("", "日本海S(3勝クラス)"): 3, ("", "町田特別(2勝クラス)"): 2, ("", "3歳以上1勝クラス"): 1,
    ("", "２歳未勝利"): 0, (None, "（G1）テスト"): 7, ("", "帝王賞(JpnI)"): 7, ("", "x(L)"): 4,
    # 地方の格 (C1/C2…) は JRA の格に対応しないので不明
    ("", "C12組"): None, ("", "てんびん座特別(C)"): None, ("", "C2四　五六　ア(C2五)"): None,
    # 「オープン」だけでは読まない (リニューアルオープン記念は3勝クラスの特別)
    ("", "阪神競馬場リニューアルオープン記念"): None,
    # grade 列が読めれば名前より優先
    ("１勝クラス", "x(GI)"): 1, ("G3", "3歳以上1勝クラス"): 5,
    ("", "障害4歳以上OP(GI)"): None,
}.items():
    check(f"class_level({g!r},{n!r})", class_level(g, n), want)

# ── recent_form_features ──
nan = float("nan")
r = recent_form_features([], 3)
check("デビュー form", r["form_cls5"], nan)
check("デビュー tb", r["tb_min5"], nan)
check("デビュー has", r["has_past_runs"], 0)

runs = [  # 新しい順
    {"finish_position": 2, "finish_time_seconds": 95.3, "win_time": 95.0, "class_level": 4},
    {"finish_position": 1, "finish_time_seconds": 96.0, "win_time": 96.0, "class_level": 2},
    {"finish_position": 10, "finish_time_seconds": 0, "win_time": 97.0, "class_level": None},
    {"finish_position": 5, "finish_time_seconds": 101.0, "win_time": 97.0, "class_level": 3},
    {"finish_position": 3, "finish_time_seconds": 98.0, "win_time": None, "class_level": 2},
    {"finish_position": 1, "finish_time_seconds": 90.0, "win_time": 90.0, "class_level": 7},  # 6走前: 使わない
]
r = recent_form_features(runs, 2)
want_form = ((2 / 18 - 0.06 * 2) + (1 / 18 - 0.0) + (10 / 18 - 0.0)
             + (5 / 18 - 0.06 * 1) + (3 / 18 - 0.0)) / 5
check("form_cls5", r["form_cls5"], want_form)
# 0.3秒 / 0秒(勝ち) / タイム0は除外 / 4秒→3秒で頭打ち / 勝ち時計なしは除外 → 最小 0
check("tb_min5", r["tb_min5"], 0.0)
check("has_past_runs", r["has_past_runs"], 1)
r = recent_form_features(runs[2:3], None)   # 今回の格が不明・過去の格も不明 → クラス差0
check("格不明 form", r["form_cls5"], 10 / 18)
check("タイム欠けだけ tb", r["tb_min5"], nan)
# 今回の格が不明 (障害戦など) なら、過去の走の格が分かっていても補正しない
r = recent_form_features(runs[:2], None)
check("今回の格不明 → 補正なし", r["form_cls5"], (2 / 18 + 1 / 18) / 2)
r = recent_form_features(runs[:2], float("nan"))
check("今回の格 NaN → 補正なし", r["form_cls5"], (2 / 18 + 1 / 18) / 2)
r = recent_form_features([{"finish_position": 4, "finish_time_seconds": 100.0,
                           "win_time": 96.0, "class_level": 0}], 0)
check("3秒で頭打ち", r["tb_min5"], 3.0)
r = recent_form_features([{"finish_position": 4, "finish_time_seconds": nan,
                           "win_time": nan, "class_level": nan}], 1)   # pandas の NaN 混入
check("NaN 混入 tb", r["tb_min5"], nan)
check("NaN 混入 form", r["form_cls5"], 4 / 18)

# ── winner_times: 勝ち馬 = 1着馬のタイム。1着の行が無い・1着のタイムが欠けたら入れない ──
wt = winner_times([
    ("A", 1, 95.0), ("A", 2, 95.3), ("A", 3, 94.0),      # 降着などで1着より速い行があっても1着馬
    ("B", 2, 96.0), ("B", 5, 97.0),                      # 1着の行が無い (地方戦・結果の欠け)
    ("C", 1, 0), ("C", 2, 98.0),                         # 1着のタイムが 0
    ("D", 1, 99.1), ("D", 1, 99.1), ("D", 3, 99.5),      # 1着同着
    ("E", "1", "100.2"), ("F", float("nan"), 90.0), ("G", 1, None), ("H", 1, float("nan")),
])
check("winner A", wt.get("A"), 95.0)
check("winner B なし", wt.get("B"), None)
check("winner C なし", wt.get("C"), None)
check("winner D 同着", wt.get("D"), 99.1)
check("winner E 文字列", wt.get("E"), 100.2)
check("winner F/G/H なし", (wt.get("F"), wt.get("G"), wt.get("H")) == (None, None, None), True)


# ── 推論側の DB 経路: 基準日が空の時に未来の走を混ぜない / 休養日数は確定走だけ ──
def _db_tests():
    import sqlite3
    import tempfile
    import database
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "t.db")
    c = sqlite3.connect(path)
    c.executescript("""
      CREATE TABLE races (race_id TEXT PRIMARY KEY, race_date TEXT, grade TEXT, race_name TEXT);
      CREATE TABLE results (race_id TEXT, horse_id TEXT, finish_position INTEGER,
                            finish_time_seconds REAL, margin TEXT);
    """)
    c.executemany("INSERT INTO races VALUES (?,?,?,?)", [
        ("P1", "2026-01-10", "２勝クラス", "x"),     # 2着 (勝ち馬の行あり)
        ("P2", "2026-02-10", "", "y"),              # 1着の行が無い (この馬の行だけ)
        ("P3", "2026-03-01", "", "天皇賞(秋)(GI)"),   # 格は名前から
        ("T",  "2026-04-01", "２勝クラス", "z"),     # 対象レース
        ("T0", "", "２勝クラス", "z"),                # 日付が空の対象レース
        ("L1", "2026-05-01", "G1", "w"),            # 対象より後の走 (混ぜてはいけない)
        ("R",  "2026-03-31", "２勝クラス", "v"),     # 対象の前日の重複登録 (着順0)
    ])
    c.executemany("INSERT INTO results VALUES (?,?,?,?,?)", [
        ("P1", "h", 2, 95.4, "1.1/4"), ("P1", "w1", 1, 95.0, ""),
        ("P2", "h", 3, 97.0, "クビ"),
        ("P3", "h", 1, 118.0, ""), ("P3", "w3", 2, 118.2, "1"),
        ("T", "h", 4, 99.0, "2"), ("T0", "x", 4, 99.0, "2"),
        ("L1", "h", 1, 90.0, ""),
        ("R", "h", 0, 0, ""),
    ])
    c.commit()
    c.close()
    old = database.DB_PATH
    database.DB_PATH = path
    try:
        fb = FeatureBuilder()
        r = fb._get_recent_form("h", "T", "2026-04-01")
        want = ((1 / 18 - 0.06 * (7 - 2)) + (3 / 18 - 0.0) + (2 / 18 - 0.0)) / 3
        check("DB form_cls5", r["form_cls5"], want)
        check("DB tb_min5 (P2 は勝ち馬不明で除外)", r["tb_min5"], 0.0)
        check("DB has_past_runs", r["has_past_runs"], 1)
        # 日付が空でも race_id から引き直す
        r2 = fb._get_recent_form("h", "T", "")
        check("DB 日付空→引き直し", r2["form_cls5"], want)
        # 引き直しても空 → 過去走なし (学習側と同じ)。未来の L1 や対象自身を混ぜない
        r3 = fb._get_recent_form("h", "T0", "")
        check("DB 日付不明→過去走なし", r3["has_past_runs"], 0)
        m3 = fb._get_margin_avg("h", "", 5, race_id="T0")
        check("DB 日付不明→着差は既定値", m3["margin_avg"], 5.0)
        m1 = fb._get_margin_avg("h", "2026-04-01", 5)
        check("DB margin_avg", m1["margin_avg"], (0.0 + 0.15 + 1.25) / 3)
        # 休養日数: 前日の重複登録 (着順0) は前走にしない → 前走は P3 (3/1) で31日
        check("DB rest_days (着順0を飛ばす)", fb._get_rest_days("h", "2026-04-01"), 31)
    finally:
        database.DB_PATH = old


_db_tests()

if FAILS:
    print(f"❌ {len(FAILS)} 件失敗")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print(f"✅ 全テスト通過 (着差表記 {len(MARGINS)}種 ×2経路 + 集計/格/近5走/勝ち馬タイム/DB経路)")
