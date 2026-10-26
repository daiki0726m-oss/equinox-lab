#!/usr/bin/env python3
"""馬場差補正スピード指数 V2d / 芝ダ替わり / 点数表 v3 (col_map・flags) の単体テスト (#168)。

  python3 scripts/test_si_variant.py        (失敗があれば exit 1)

対象:
  ml/speed_variant.py   race_table / fit_table / day_offsets / run_values / row_features /
                        surface_switch_features / legacy_si / DbSiVariant (推論側) / TrainSiVariant (学習側)
  ml/scorecard.py       col_map と flags (無い pkl は従来と完全に同じ点)
小さな合成 DB (一時ファイル) を作って、学習側と推論側が同じ値を出すことも確かめる。
"""
import json
import math
import os
import sqlite3
import sys
import tempfile

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ml import speed_variant as sv  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(f"{name} {detail}")


def close(a, b, tol=1e-9):
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return abs(float(a) - float(b)) <= tol


# ── 部品 ──
check("dist_factor", list(sv.dist_factor([1000, 1199, 1200, 1599, 1600, 1999, 2000, 2499, 2500, 3600]))
      == [15, 15, 12, 12, 10, 10, 8.5, 8.5, 7, 7])
for name, want in [("2歳新馬", "2"), ("2歳未勝利", "2"), ("3歳以上1勝クラス", "古"), ("4歳以上2勝クラス", "古"),
                   ("3歳未勝利", "3"), ("3歳1勝クラス", "3"), ("東京スポーツ杯2歳S(GII)", "2"),
                   ("天皇賞(秋)(GI)", None), ("", None), (None, None)]:
    check(f"age_group_from_name({name})", sv.age_group_from_name(name) == want, sv.age_group_from_name(name))
for a, m, want in [("2", 6, "a"), ("2", 8, "a"), ("2", 9, "b"), ("2", 11, "c"), ("3", 3, "a"), ("3", 4, "b"),
                   ("3", 7, "c"), ("古", 5, "")]:
    check(f"_season({a},{m})", sv._season(a, m) == want)

# legacy_si は fast_train._SpeedIndexCache.si と同じ式・同じ丸め
import fast_train as ft  # noqa: E402


class _FixedBT(ft._SpeedIndexCache):
    def __init__(self, bt):
        dict.__init__(self)
        self._bt_fixed = bt

    def base_time(self, *a, **k):
        return self._bt_fixed


rng = np.random.default_rng(0)
for _ in range(2000):
    bt = float(rng.uniform(50, 230))
    t = round(float(bt + rng.normal(0, 2)), 1)
    d = int(rng.choice([1000, 1150, 1200, 1400, 1600, 1800, 2000, 2200, 2400, 2500, 3000, 3600]))
    tc = str(rng.choice(["良", "稍重", "重", "不良", "", "?"]))
    a = sv.legacy_si(bt, t, d, tc)
    b = _FixedBT(bt).si({"venue": "x", "distance": d, "surface": "芝", "tc": tc, "t": t}, "2026-01-01")
    if a != b:
        FAILS.append(f"legacy_si {bt} {t} {d} {tc}: {a} != {b}")
        break
check("legacy_si(bt=0)", sv.legacy_si(0, 60.0, 1200, "良") == 0)

# ── 芝ダ替わり ──
T, D, J = ({"surface": "芝", "is_jump": False}, {"surface": "ダート", "is_jump": False},
           {"surface": "芝", "is_jump": True})
cases = [
    ([T, T, T], "ダート", False, (1.0, 1.0)),          # 芝3走 → 初ダート
    ([T, D, T], "ダート", False, (0.0, 2 / 3)),        # ダート経験あり
    ([D], "ダート", False, (0.0, 0.0)),
    ([J, J, T], "ダート", False, (1.0, 1.0)),          # 障害は数えない (平地は芝1走だけ)
    ([J, J], "ダート", False, (0.0, 0.0)),             # 平地走なし
    ([], "芝", False, (0.0, 0.0)),
    ([T, T], "ダート", True, (0.0, 0.0)),              # 今回が障害
    ([T, T], "", False, (0.0, 0.0)),                   # 今回の芝ダ不明
    ([D, D, D, D, D, T, T], "芝", False, (0.0, 1.0)),  # 近5走は全部ダート、芝は6走前以前
    ([T, D, D, D, D, D, D], "ダート", False, (0.0, 0.2)),
]
for i, (past, cur, jump, want) in enumerate(cases):
    got = sv.surface_switch_features(past, cur, jump)
    check(f"surface_switch case{i}", close(got["sf_first_switch"], want[0]) and close(got["sf_other_share5"], want[1]),
          f"{got} want {want}")

# ── row_features / aggregate ──
lk = {("h", "r1"): 90.0, ("h", "r3"): 70.0}
runs = [{"race_id": "r1"}, {"race_id": "r2"}, {"race_id": "r3"}]
out = sv.row_features("h", runs, lk, lambda s: 50.0)
check("row_features", close(out["si_avg_v"], (90 + 50 + 70) / 3) and out["si_latest_v"] == 90.0
      and out["si_v_nfb"] == 1, out)
out = sv.row_features("h", [], lk, lambda s: 50.0)
check("row_features empty", out == {"si_avg_v": 0.0, "si_latest_v": 0.0, "si_v_nfb": 0}, out)


# ══════════════════════════════════════════════════════════════════════
# 合成データ: 既知の μ・κ・δ から時計を作り、当てはめと学習側/推論側の一致を見る
# ══════════════════════════════════════════════════════════════════════
def synth(seed=1):
    rng = np.random.default_rng(seed)
    venues = ["東京", "中山"]
    dists = {"芝": [1600, 2000], "ダート": [1400, 1800]}
    grades = ["未勝利", "１勝クラス", "OP"]
    races, results, horses = [], [], {}
    hid = 0
    dates = pd.date_range("2020-01-04", periods=60, freq="7D").strftime("%Y-%m-%d").tolist()
    pool = [f"H{i:04d}" for i in range(400)]
    for h in pool:
        horses[h] = int(rng.choice([2016, 2017, 2018]))
    for di, dt in enumerate(dates):
        for v in venues:
            day_eff = {"芝": rng.normal(0, 1.0), "ダート": rng.normal(0, 1.0)}
            for rn in range(1, 13):
                surf = "芝" if rn % 2 else "ダート"
                dist = int(rng.choice(dists[surf]))
                g = str(rng.choice(grades))
                name = {"未勝利": "3歳未勝利", "１勝クラス": "3歳以上1勝クラス", "OP": "テストS"}[g]
                if rn == 12 and di % 9 == 0:
                    name = "障害4歳以上未勝利"     # 障害 (平地の当てはめから外れる)
                rid = f"{dt.replace('-', '')}{venues.index(v)}{rn:02d}"
                races.append({"race_id": rid, "race_date": dt, "venue": v, "race_number": rn,
                              "race_name": name, "grade": g, "distance": dist, "surface": surf,
                              "track_condition": "良"})
                base = dist * 0.06 - {"未勝利": 0, "１勝クラス": 0.4, "OP": 0.9}[g] - day_eff[surf] * 0.1
                entrants = rng.choice(pool, 10, replace=False)
                times = np.sort(np.round(base + rng.exponential(1.0, 10), 1))
                for k, h in enumerate(entrants):
                    results.append({"race_id": rid, "horse_id": str(h), "horse_number": k + 1,
                                    "finish_position": k + 1, "finish_time_seconds": float(times[k])})
                hid += 1
    races = pd.DataFrame(races)
    results = pd.DataFrame(results)
    # 欠け: (1) ある日のレース番号の歯抜け (2) ある日のある芝ダのレースの結果欠け
    gap_rid = races[(races.race_date == dates[10]) & (races.venue == "東京") & (races.race_number == 5)].race_id.iloc[0]
    races = races[races.race_id != gap_rid]
    results = results[results.race_id != gap_rid]
    miss_rid = races[(races.race_date == dates[20]) & (races.venue == "中山") & (races.race_number == 3)].race_id.iloc[0]
    results = results[~((results.race_id == miss_rid) & (results.finish_position > 1))]
    # (3) ある日の末尾のレース (11R・12R) が DB に無い (#168 レビュー: 旧規則 1..最大 は見逃した)
    tail_rids = races[(races.race_date == dates[30]) & (races.venue == "中山")
                      & (races.race_number >= 11)].race_id.tolist()
    races = races[~races.race_id.isin(tail_rids)]
    results = results[~results.race_id.isin(tail_rids)]
    results["birth_year"] = results.horse_id.map(horses)
    return races.reset_index(drop=True), results.reset_index(drop=True), horses, dates, gap_rid, miss_rid


races, results, horses, dates, gap_rid, miss_rid = synth()
rt = sv.race_table(races, results)
fitd = sv.fit_table(rt, dates[45])
table = sv.make_table(fitd, dates[45], c=0.0)
# κ̂ はレース数重みで芝ダごとに平均0
fit = rt[rt.flat & rt.has_t3 & (rt.race_date <= dates[45])]
for s in ("芝", "ダート"):
    m = fit[fit.surface == s]
    kk = m.cls.map(table["kap"]).mean()
    check(f"κ̂ の {s} 平均0", abs(kk) < 1e-9, kk)
# 正規化は μ̂+κ̂ (当てはめ値) を変えない
fit0 = sv.fit_table(rt, dates[45], normalize_surface=False)
a = fit.course.map(fitd["mu"]) + fit.cls.map(fitd["kap"])
b = fit.course.map(fit0["mu"]) + fit.cls.map(fit0["kap"])
check("正規化は μ̂+κ̂ を変えない", np.allclose(a, b, atol=1e-9), float((a - b).abs().max()))
# そろっていない日
day_gap = [f"{dates[10]}|東京|芝", f"{dates[10]}|東京|ダート"]
day_miss = f"{dates[20]}|中山|芝"     # 3R (奇数=芝) の結果が欠け
day_ok = f"{dates[20]}|中山|ダート"
comp = rt.groupby("day").complete.first()
check("番号の歯抜けの日はどちらの芝ダもそろっていない", not comp[day_gap[0]] and not comp[day_gap[1]])
day_tail = [f"{dates[30]}|中山|芝", f"{dates[30]}|中山|ダート"]
check("末尾のレースが無い日 (1..10R だけ) もそろっていない", not comp[day_tail[0]] and not comp[day_tail[1]])
check("同じ日の別の場はそろっている", comp[f"{dates[30]}|東京|芝"] and comp[f"{dates[30]}|東京|ダート"])
check("結果欠けの芝ダだけそろっていない", (not comp[day_miss]) and comp[day_ok])
doff = sv.day_offsets(rt, table)
check("そろっていない日は δ̂ を作らない", day_miss not in doff and day_ok in doff and day_gap[0] not in doff
      and day_tail[0] not in doff and day_tail[1] not in doff)
# δ̂ = その日の残差の平均 (そのレース自身も含む)
res = sv.race_residuals(rt, table)
d0 = pd.Series(res[rt.day == day_ok]).dropna().mean()
check("δ̂ はその日の残差の平均", close(doff[day_ok], d0), (doff[day_ok], d0))
# 走の V2d の式と c のずらし
runs = results[results.finish_time_seconds > 0][["race_id", "horse_id", "finish_time_seconds"]].reset_index(drop=True)
v0, dirt0 = sv.run_values(runs, rt, table, doff, res, c=0.0)
v5, _ = sv.run_values(runs, rt, table, doff, res, c=5.0)
pos = rt.set_index("race_id")
j = int(np.flatnonzero(np.isfinite(v0))[0])
r = runs.iloc[j]
rr = pos.loc[r.race_id]
want = 80 - sv.dist_factor(rr.distance) * r.finish_time_seconds - table["mu"][rr.course] - doff[rr.day]
check("V2d の式", close(v0[j], float(want)), (v0[j], want))
dd = v5 - v0
ok = np.isfinite(v0)
check("c はダートの走だけ・V2d の走だけ", np.allclose(dd[ok & dirt0], 5.0) and np.allclose(dd[ok & ~dirt0], 0.0)
      and np.array_equal(np.isfinite(v5), ok))
jump_runs = runs.race_id.map(pos.is_jump).to_numpy()
check("障害の走は V2d を作らない", not np.isfinite(v0[jump_runs]).any())
check("そろっていない日の走は V2d を作らない",
      not np.isfinite(v0[runs.race_id.map(pos.day).isin([day_miss] + day_gap).to_numpy()]).any())

# ── 基準表の保存・読込・指紋 ──
tmpd = tempfile.mkdtemp()
tp = os.path.join(tmpd, "si_variant_table.json")
with open(tp, "w", encoding="utf-8") as f:
    json.dump(table, f, ensure_ascii=False, indent=1)
t2 = sv.load_table(tp)
check("基準表の往復", t2["table_hash"] == table["table_hash"] and t2["mu"] == table["mu"])
bad = dict(table)
bad["dirt_shift_c"] = 3.0
tp2 = os.path.join(tmpd, "bad.json")
with open(tp2, "w", encoding="utf-8") as f:
    json.dump(bad, f, ensure_ascii=False)
try:
    sv.load_table(tp2)
    FAILS.append("指紋の合わない基準表を読めてしまった")
except ValueError:
    pass
# 表を作った時の定義がコードと違う → 例外 (プロセス内で定数・規則を書き換えても素通りしない)
check("表に定義と定義の指紋がある", t2.get("definition_hash") == sv.definition_hash()
      and all(k in t2 for k in sv.DEF_KEYS))


def _expect_def_error(label, mutate, restore):
    mutate()
    try:
        sv.load_table(tp)
        FAILS.append(f"定義が違うのに基準表を読めてしまった ({label})")
    except ValueError as e:
        if "定義" not in str(e):
            FAILS.append(f"定義違いの例外文が違う ({label}): {e}")
    finally:
        restore()


_sd, _df, _gm, _rv, _fc = (dict(sv.SEASON_DEF), sv.DIST_FACTORS, dict(sv.GRADE_MAP), dict(sv.RULE_VERSIONS),
                           sv.FULL_CARD)
_expect_def_error("時期区分", lambda: sv.SEASON_DEF.__setitem__("2", ((7, "a"), (10, "b"), (12, "c"))),
                  lambda: (sv.SEASON_DEF.clear(), sv.SEASON_DEF.update(_sd)))
_expect_def_error("距離係数", lambda: setattr(sv, "DIST_FACTORS", (15.0, 12.0, 10.0, 8.0, 7.0)),
                  lambda: setattr(sv, "DIST_FACTORS", _df))
_expect_def_error("格の対応", lambda: sv.GRADE_MAP.__setitem__("OP", "OP2"),
                  lambda: (sv.GRADE_MAP.clear(), sv.GRADE_MAP.update(_gm)))
_expect_def_error("規則の版", lambda: sv.RULE_VERSIONS.__setitem__("jump", "x"),
                  lambda: (sv.RULE_VERSIONS.clear(), sv.RULE_VERSIONS.update(_rv)))
_expect_def_error("1日の番組数", lambda: setattr(sv, "FULL_CARD", 11), lambda: setattr(sv, "FULL_CARD", _fc))
_orig_age = sv.age_group_from_name
_expect_def_error("年齢区分のプローブ",
                  lambda: setattr(sv, "age_group_from_name", lambda n: "古" if n and "3歳" in n else _orig_age(n)),
                  lambda: setattr(sv, "age_group_from_name", _orig_age))
_orig_jump = sv.is_jump_race
_expect_def_error("障害判定のプローブ",
                  lambda: setattr(sv, "is_jump_race", lambda n, *a, **k: "障害" in (n or "")),
                  lambda: setattr(sv, "is_jump_race", _orig_jump))
check("元に戻せば読める", sv.load_table(tp)["table_hash"] == table["table_hash"])
# 定義を持たない古い形の表 (指紋は合っていても) は読まない
legacy = {k: v for k, v in table.items() if k not in sv.DEF_KEYS and k != "definition_hash"}
legacy["table_hash"] = sv.table_hash(legacy)
tp3 = os.path.join(tmpd, "legacy.json")
with open(tp3, "w", encoding="utf-8") as f:
    json.dump(legacy, f, ensure_ascii=False)
try:
    sv.load_table(tp3)
    FAILS.append("定義の無い古い形の基準表を読めてしまった")
except ValueError:
    pass

# ── 学習側 (TrainSiVariant / fast_train) と推論側 (DbSiVariant / DbLegacySpeedIndex) の一致 ──
dbp = os.path.join(tmpd, "t.db")
con = sqlite3.connect(dbp)
con.executescript("""
CREATE TABLE races (race_id TEXT PRIMARY KEY, race_date TEXT NOT NULL, venue TEXT NOT NULL,
  race_number INTEGER NOT NULL, race_name TEXT, grade TEXT, distance INTEGER NOT NULL,
  surface TEXT NOT NULL, track_condition TEXT);
CREATE TABLE results (race_id TEXT, horse_id TEXT, horse_number INTEGER, finish_position INTEGER,
  finish_time_seconds REAL);
CREATE TABLE horses (horse_id TEXT PRIMARY KEY, birth_year INTEGER);
""")
races.to_sql("races", con, if_exists="append", index=False)
results.drop(columns=["birth_year"]).to_sql("results", con, if_exists="append", index=False)
pd.DataFrame({"horse_id": list(horses), "birth_year": list(horses.values())}).to_sql(
    "horses", con, if_exists="append", index=False)
con.commit()
con.close()
import database  # noqa: E402
database.DB_PATH = dbp
tsv = sv.TrainSiVariant(races, results, t2)
rr2 = results.merge(races[["race_id", "race_date", "venue", "distance", "surface", "track_condition"]], on="race_id")
si_cache = ft.build_speed_index_cache(rr2.copy(), races.copy())
dsv = sv.DbSiVariant(t2, database.get_db)
n_cmp = n_bad = 0
rng = np.random.default_rng(3)
for hid in rng.choice(sorted(set(results.horse_id)), 40, replace=False):
    for T in (dates[30], dates[50], dates[59]):
        si_runs = [s for s in si_cache.get(hid, []) if s["race_date"] < T][:5]
        a = tsv.row_features(hid, si_runs, lambda s, _T=T: si_cache.si(s, _T))
        b = dsv.row_features(hid, T)
        n_cmp += 1
        if not (close(a["si_avg_v"], b["si_avg_v"], 1e-9) and close(a["si_latest_v"], b["si_latest_v"], 1e-9)
                and a["si_v_nfb"] == b["si_v_nfb"]):
            n_bad += 1
            if n_bad <= 3:
                FAILS.append(f"学習側≠推論側 {hid} {T}: {a} vs {b}")
        # 現行の指数 (埋める側) も学習側と同じ値
        for s in si_runs:
            x, y = si_cache.si(s, T), dsv.legacy.si(s, T)
            if x != y:
                FAILS.append(f"現行の指数が学習側と違う {hid} {s['race_id']} {T}: {x} vs {y}")
                break
check("学習側と推論側を照合した", n_cmp == 120 and n_bad == 0, (n_cmp, n_bad))
check("V2d で埋めた走が実際にある", sum(1 for k in tsv.lookup) > 1000)

# ── 点数表: 古い pkl は従来と完全に同じ / col_map / flags ──
from ml import scorecard as sc  # noqa: E402
mp = os.path.join(ROOT, "models", "scorecard.pkl")
if os.path.exists(mp):
    # models/ の点数表が v3 (col_map 付き) でも、ここでは「差し替え無し」の基準として扱う。
    # (v3 を置いたまま走らせると合成データに si_avg_v が無く KeyError になっていた)
    m_old = dict(sc.load_model(mp))
    m_old.pop("col_map", None)
    m_old.pop("si_variant", None)
    rngf = np.random.default_rng(5)
    n = 60
    df = pd.DataFrame({"race_id": np.repeat(["A", "B", "C"], n // 3)})
    for c, *_ in sc.FEATS:
        df[c] = np.where(rngf.random(n) < 0.15, 0.0, rngf.uniform(0.1, 100, n))
    for c, *_ in sc.LIN_FEATS:
        df[c] = rngf.normal(0, 1, n)
    df[sc.DEBUT_COL] = (rngf.random(n) > 0.1).astype(int)
    s0, r0 = sc.score_with_reasons(df, m_old)
    m1 = dict(m_old)
    m1["col_map"] = {}
    m1["flags"] = {}
    s1, r1 = sc.score_with_reasons(df, m1)
    check("col_map/flags が空なら同じ点", np.array_equal(s0, s1) and r0 == r1)
    df2 = df.copy()
    df2["si_avg_v"] = df2["si_avg"]
    df2["si_latest_v"] = df2["si_latest"]
    df2["si_avg"] = 1.0
    df2["si_latest"] = 1.0
    m2 = dict(m_old)
    m2["col_map"] = {"si_avg": "si_avg_v", "si_latest": "si_latest_v"}
    s2, r2 = sc.score_with_reasons(df2, m2)
    check("col_map で列を差し替えても同じ値なら同じ点・同じ内訳", np.allclose(s0, s2) and r0 == r2)
    check("required_columns は差し替え先", "si_avg_v" in sc.required_columns(m2)
          and "si_avg" not in sc.required_columns(m2))
    m2d = sc.without_col_map(dict(m2, si_variant={"table_hash": "x"}), "テスト")
    check("代用モデルは現行の si_avg を読む", "si_avg" in sc.required_columns(m2d)
          and "si_avg_v" not in sc.required_columns(m2d) and m2d["_degraded"] == "テスト"
          and m2.get("col_map"))
    s2d, _ = sc.score_with_reasons(df, m2d)
    check("代用モデルの点 = 差し替え無しの点", np.allclose(s0, s2d))
    m3 = dict(m_old)
    m3["flags"] = {"sf_first_switch": -0.5, "sf_other_share5": -0.2}
    df3 = df.copy()
    df3["sf_first_switch"] = 0.0
    df3["sf_other_share5"] = 0.0
    df3.loc[3, "sf_first_switch"] = 1.0
    df3.loc[3, "sf_other_share5"] = 0.6
    df3["surface_turf"] = 0
    s3, r3 = sc.score_with_reasons(df3, m3, top_n=40)
    check("flags の点", close(s3[3] - s0[3], (-0.5 - 0.2 * 0.6) * 10, 1e-9) and np.allclose(np.delete(s3, 3), np.delete(s0, 3)))
    labs = {x["label"]: x["bin"] for x in r3[3]}
    check("flags の内訳 (初ダート / 芝ダ替わり)", labs.get("初ダート") == "替わり初戦" and labs.get("芝ダ替わり") == "他馬場60%", labs)
    check("required_columns に flags", "sf_first_switch" in sc.required_columns(m3))
    check("内訳の表示名は約6文字以内", all(len(n) <= 6 for _, n, _ in sc.FLAG_FEATS))
else:
    print("⚠️ models/scorecard.pkl が無い — 点数表の後方互換テストを飛ばした")

# ── 区分点のロジスティック回帰は収束させる (#168 レビュー: 既定 tol=1e-4 は約30反復で止まっていた) ──
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import train_scorecard as _ts  # noqa: E402
_rng = np.random.default_rng(11)
_lab = pd.DataFrame({c: _rng.choice(["1位", "2-3位", "4-6位", "7位以下"], 3000) for c, *_ in sc.FEATS[:4]})
_y = (_rng.random(3000) < 0.3).astype(int)
_info = {}
_ts.fit(_lab, _y, info=_info)
check("区分点の回帰は収束条件 tol=1e-8 で解く", _info.get("tol") == 1e-8 and _info.get("n_iter", 0) > 0, _info)
try:
    _ts.fit(_lab, _y, max_iter=1)
    FAILS.append("収束しない区分点の回帰が例外にならなかった")
except RuntimeError:
    pass

if FAILS:
    print(f"❌ {len(FAILS)} 件失敗")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("✅ test_si_variant: 全部通過")
