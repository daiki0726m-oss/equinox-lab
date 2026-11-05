#!/usr/bin/env python3
"""点数表の内訳の表示 (#169) のテスト。DB 不要 (合成データ + models/scorecard.pkl)。

  python3 scripts/test_scorecard_display.py      # 失敗があれば exit 1

確かめること
  1. 区分の表示名は区分境界から作られ、段数と合う。内部表記 ("5/5") は出ない
  2. 段数・名前の不一致を check_display_bins が検出する (名前の無い区分は内訳に出さない)
  3. レース内の全頭が同じ区分の項目は内訳に出さない (新馬戦の「データなし」「休み明け」)
  4. 過去走の無い馬の前走由来の項目は「JRA初出走」、走って3着内0回は「3着内なし(N走)」
  5. 点数そのもの (合計) は表示の変更で変わらない
  6. 画面の SI (display_si / si_rank) は内訳の「平均スピード指数」と同じ値・同じ順位
  7. (手直し) 順位は 0 (データなし) を母集団に入れない・丸めた値で同順位を作らない・
     取消馬 (_scratched) は表示の V2d も 0・表示処理の失敗で点数を止めない・
     古いキャッシュの内部表記と新馬戦の全頭共通項目を export でそろえる
"""
import os
import re
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ml import scorecard as sc  # noqa: E402
from ml import si_display as sd  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(("  ✅ " if ok else "  ❌ ") + name + ("" if ok else f"  {detail}"))
    if not ok:
        FAILS.append(name)


RAW = re.compile(sc.RAW_BIN_RE)
m = sc.load_model()
if not m:
    print("⚠️ models/scorecard.pkl が無い — テストを飛ばす")
    sys.exit(0)

# ── 1. 表示名は区分境界から ──
print("[1] 表示名")
for col, edges in m["spec"].items():
    names = sc.bin_display_names(col, edges)
    check(f"{col}: 段数 {len(edges) - 1} = 名前 {len(names or [])}",
          names is not None and len(names) == len(edges) - 1, names)
check("現行の点数表は検査を通る", sc.check_display_bins(m) == [], sc.check_display_bins(m))
check("道中の位置取り (v3 は5段) に5つの名前",
      sc.bin_display_names("avg_pos_ratio", m["spec"]["avg_pos_ratio"]) == ["先行", "好位", "中団", "中団後ろ", "後方"])
# キャリア戦数は「近10走の確定走の数 / 10」(#166)。0.1 = 1戦
check("キャリア戦数は近10走の数で名付ける",
      sc.bin_display_names("race_experience", [-np.inf, 0.1, 0.3, 0.6, 1.0, np.inf])
      == ["1戦以下", "2〜3戦", "4〜6戦", "7戦以上", None])
check("休養日数は日数 (最後は休み明け)",
      sc.bin_display_names("rest_days", [-np.inf, 21 / 365, 29 / 365, 62 / 365, 120 / 365, np.inf])
      == ["21日以内", "22〜29日", "30〜62日", "63〜120日", "4ヶ月超(休み明け)"])
# 言葉が重なる段 (v1/v2 の位置取り) は境界の % で名付ける
_v2 = sc.bin_display_names("avg_pos_ratio", [-np.inf, 0.438, 0.5, 0.533, np.inf])
check("位置取りの言葉が重なる時は % で名付ける (重ならない)", len(set(_v2)) == 4 and not any(RAW.match(x) for x in _v2), _v2)

# ── 2. 検出 ──
print("[2] 不一致の検出")
_saved = dict(sc._BIN_NAMERS)
try:
    sc._BIN_NAMERS.pop("avg_pos_ratio")
    probs = sc.check_display_bins(m)
    check("名前を作れない項目を検出", any("avg_pos_ratio" in p for p in probs), probs)
    sc._BIN_NAMERS["rest_days"] = lambda e: ["a", "b"]
    probs = sc.check_display_bins(m)
    check("段数と名前の数の違いを検出", any("rest_days" in p and "表示名 2 個" in p for p in probs), probs)
    sc._BIN_NAMERS["front_rate"] = lambda e: ["1/5", "2/5", "3/5", "4/5"]
    probs = sc.check_display_bins(m)
    check("内部表記の名前を検出", any("front_rate" in p and "内部表記" in p for p in probs), probs)
    check("名前の無い区分は None (内部表記を返さない)", sc.display_bin("avg_pos_ratio", "5/5", None) is None)
finally:
    sc._BIN_NAMERS.clear()
    sc._BIN_NAMERS.update(_saved)

# ── 合成データ ──
rng = np.random.default_rng(7)


def make_race(rid, n, debut=False):
    d = {"race_id": [rid] * n}
    for c, *_ in sc.FEATS:
        d[c] = rng.uniform(0.05, 0.9, n)
    d["si_avg_v"] = rng.uniform(60, 100, n)
    d["si_latest_v"] = rng.uniform(60, 100, n)
    d["form_cls5"] = rng.normal(0.4, 0.1, n)
    d["tb_min5"] = rng.uniform(0, 2, n)
    d[sc.DEBUT_COL] = np.ones(n, int)
    d["race_experience"] = rng.choice([0.3, 0.5, 1.0], n)
    d["jockey_change"] = rng.choice([0.0, 1.0], n)
    d["dist_scope"] = np.zeros(n)
    df = pd.DataFrame(d)
    if debut:   # 新馬戦: 過去走なし → 前走由来の項目は既定値、近走の率は0
        for c in ("top3_rate_10r", "avg_last_3f", "si_avg_v", "si_latest_v", "dist_top3_rate",
                  "course_top3_rate", "same_course_top3_rate", "horse_wet_top3_rate"):
            df[c] = 0.0
        df["rest_days"] = 0.5
        df["distance_diff"] = 0.0
        df["jockey_change"] = 0.0
        df["impost_diff"] = 0.0
        df["avg_pos_ratio"] = 0.5
        df["front_rate"] = 0.0
        df["race_experience"] = 0.0
        df["graded_exp"] = 0.0
        df["form_cls5"] = np.nan
        df["tb_min5"] = np.nan
        df["dist_scope"] = 2.0
        df[sc.DEBUT_COL] = 0
    return df


A = make_race("A", 12, debut=True)
B = make_race("B", 10)
# B の0番 = JRA初出走 (混合戦に初出走の馬)
for c in ("top3_rate_10r", "avg_last_3f", "si_avg_v", "si_latest_v", "dist_top3_rate"):
    B.loc[0, c] = 0.0
B.loc[0, ["rest_days", "distance_diff", "jockey_change", "avg_pos_ratio", "front_rate",
          "race_experience", "dist_scope"]] = [0.5, 0.0, 0.0, 0.5, 0.0, 0.0, 2.0]
B.loc[0, sc.DEBUT_COL] = 0
# B の1番 = 5走して3着内0回 / B の2番 = この距離で走って3着内0回
B.loc[1, ["top3_rate_10r", "race_experience"]] = [0.0, 0.5]
B.loc[2, ["dist_top3_rate", "dist_scope"]] = [0.0, 0.0]
df = pd.concat([A, B], ignore_index=True)
pts, reasons = sc.score_with_reasons(df, m, top_n=40)

# ── 3. 全頭共通の項目 ──
print("[3] 全頭共通の項目")
a_labels = {(r["label"], r["bin"]) for i in range(12) for r in reasons[i]}
for lb in ("休養日数", "近10走の複勝率", "上がり3F平均", "前走との距離差", "乗り替わり", "道中の位置取り"):
    check(f"新馬戦の内訳に「{lb}」が出ない (全頭同じ区分)", not any(x[0] == lb for x in a_labels), a_labels)
check("新馬戦に「休み明け」が出ない", not any("休み明け" in x[1] for x in a_labels))
check("新馬戦でも差のつく項目 (騎手等) は出る", any(x[0] == "騎手の複勝率" for x in a_labels), a_labels)

# ── 4. 初出走・3着内なし ──
print("[4] 初出走 / 3着内なし")
b0 = {r["label"]: r["bin"] for r in reasons[12]}
check("混合戦の初出走馬の休養日数は「JRA初出走」", b0.get("休養日数") == sc.DEBUT_LABEL, b0)
check("初出走馬に「休み明け」と書かない", not any("休み明け" in v for v in b0.values()), b0)
b1 = {r["label"]: r["bin"] for r in reasons[13]}
check("5走して3着内0回は「3着内なし(5走)」", b1.get("近10走の複勝率") == "3着内なし(5走)", b1)
b2 = {r["label"]: r["bin"] for r in reasons[14]}
check("この距離で走って3着内0回は「3着内なし」", b2.get("この距離の実績") == "3着内なし", b2)
check("初出走馬の近10走の複勝率は「データなし」のまま", b0.get("近10走の複勝率") in (None, sc.NODATA), b0)
allbins = [r["bin"] for rs in reasons for r in rs]
check("内訳に内部表記 (N/M) が無い", not any(RAW.match(str(b)) for b in allbins),
      [b for b in allbins if RAW.match(str(b))])

# ── 5. 点数は表示に依存しない ──
print("[5] 点数")
df2 = df.copy()
df2["dist_scope"] = 2.0            # 表示にしか使わない列
pts2, _ = sc.score_with_reasons(df2, m, top_n=40)
check("表示用の列 (dist_scope) を変えても点は同じ", np.allclose(pts, pts2))
pts3, r3 = sc.score_with_reasons(df, m, top_n=4)
check("top_n を変えても点は同じ・内訳は4件以内", np.allclose(pts, pts3) and all(len(x) <= 4 for x in r3))

# ── 6. 画面の SI = 内訳の平均スピード指数 ──
print("[6] 画面の SI")
horses = [{"horse_number": i + 1, "si_avg": float(100 - i), **sd.fields_from_row(B.iloc[i], m),
           "sc_reasons": reasons[12 + i]} for i in range(len(B))]
check("V2d の点数表なら si_avg_v を書く", all("si_avg_v" in h for h in horses))
bad = 0
for h in horses:
    for r in h["sc_reasons"]:
        if r["label"] == "平均スピード指数":
            rk = sd.si_rank(horses, h)
            exp = "データなし" if rk is None else next(nm for lo, hi, nm in sc.REL_BINS if lo <= rk <= hi)
            bad += exp != r["bin"]
check("内訳「平均スピード指数」の区分 = 画面 SI のレース内順位", bad == 0, bad)
mdeg = sc.without_col_map(m, "テスト")
check("代用モード (col_map なし) では si_avg_v を書かない", sd.fields_from_row(B.iloc[1], mdeg) == {})
check("キーが無ければ旧指数を出す", sd.display_si({"si_avg": 88.0}) == 88.0)
check("キーがあれば (0 でも) その値", sd.display_si({"si_avg": 88.0, "si_avg_v": 0}) == 0.0)

# ── 7. 手直し (#169 レビュー) ──
print("[7] 手直し")
_drop = set(m.get("drop") or [])


def _rel_bin(rk):
    return "データなし" if rk is None else next(nm for lo, hi, nm in sc.REL_BINS if lo <= rk <= hi)


# 7a. 0 (データなし) の馬を順位に入れない: 負の V2d の馬の区分が点数表と同じになる
C = make_race("C", 8)
C["si_avg_v"] = [95.0, 90.0, 0.0, 0.0, 0.0, 0.0, -3.0, -5.2]
labC = sc.label(C, m["spec"], skip=_drop, col_map=m.get("col_map"))
hC = [{"horse_number": i + 1, **sd.fields_from_row(C.iloc[i], m)} for i in range(len(C))]
mis = [(i + 1, labC["si_avg"].iloc[i], _rel_bin(sd.si_rank(hC, hC[i]))) for i in range(len(C))
       if labC["si_avg"].iloc[i] != _rel_bin(sd.si_rank(hC, hC[i]))]
check("負の V2d の馬も点数表の区分と画面の順位が同じ (0 の馬を数えない)", not mis, mis)
check("-5.2 の馬は 4位 (旧実装は 0 の4頭を上に数えて 8位)", sd.si_rank(hC, hC[7]) == 4, sd.si_rank(hC, hC[7]))

# 7b. 丸めた値で同順位を作らない (88.04 と 87.96 は 1桁ではどちらも 88.0)
D = make_race("D", 8)
# (6位と7位の境目。丸めると両方 88.0 = 同順位6位になり、87.96 の馬が「4-6位」と出ていた)
D["si_latest_v"] = [99.0, 97.0, 95.0, 93.0, 91.0, 88.04, 87.96, 80.0]
labD = sc.label(D, m["spec"], skip=_drop, col_map=m.get("col_map"))
hD = [{"horse_number": i + 1, **sd.fields_from_row(D.iloc[i], m)} for i in range(len(D))]
check("キャッシュの V2d は丸めない", hD[6]["si_latest_v"] == 87.96 and hD[5]["si_latest_v"] == 88.04,
      (hD[5].get("si_latest_v"), hD[6].get("si_latest_v")))
check("1桁で同値の2頭も点数表と同じ区分 (87.96 は 7位以下)",
      _rel_bin(sd.si_rank(hD, hD[6], latest=True)) == labD["si_latest"].iloc[6] == "7位以下",
      (sd.si_rank(hD, hD[6], latest=True), labD["si_latest"].iloc[6]))
_hDr = [dict(h, si_latest_v=round(h["si_latest_v"], 1)) for h in hD]
check("(対照) 1桁に丸めると 4-6位 と出て食い違う", _rel_bin(sd.si_rank(_hDr, _hDr[6], latest=True)) == "4-6位")
check("表示は小数1桁 (fmt_si)", sd.fmt_si(hD[6]["si_latest_v"]) == 88.0)
check("投稿の整数は画面の1桁を四捨五入 (83.48→画面83.5→84 / 80.5→81)",
      sd.si_int(83.477) == 84 and sd.si_int(80.5) == 81 and sd.si_int(80.539) == 81 and sd.si_int(-5.2) == -5,
      (sd.si_int(83.477), sd.si_int(80.5), sd.si_int(80.539), sd.si_int(-5.2)))

# 7c. 取消馬 (_scratched) は表示の V2d も 0 (点数表の母集団から外した馬を画面でも数えない)
preds = [{"horse_number": 1, "si_avg_v": 101.0, "si_latest_v": 99.0, "_scratched": True},
         {"horse_number": 2, "si_avg_v": 95.0, "si_latest_v": 94.0, "_scratched": False},
         {"horse_number": 3, "si_avg": 80.0}]
n0 = sd.zero_scratched(preds)
check("取消馬の V2d だけ 0 にする", n0 == 1 and preds[0]["si_avg_v"] == 0.0 and preds[1]["si_avg_v"] == 95.0
      and "si_avg_v" not in preds[2], preds)
check("取消馬を外すと 95.0 の馬が1位", sd.si_rank(preds, preds[1]) == 1)
_noscr = [{"horse_number": 1, "si_avg_v": 101.0, "si_latest_v": 99.0}]
check("_scratched が無ければ何もしない (取消を外さない点数表と同じ)", sd.zero_scratched(_noscr) == 0
      and _noscr[0]["si_avg_v"] == 101.0)

# 7d. 表示処理の例外で点数を止めない (止めると印が従来ロジックに落ちる)
_orig_ctx, _orig_common = sc._reason_context, sc._race_common
try:
    def _boom(*a, **k):
        raise RuntimeError("表示テスト")
    sc._reason_context = _boom
    p_e, r_e = sc.score_with_reasons(df, m, top_n=40)
    check("_reason_context が落ちても点数は同じ・内訳は空", np.allclose(p_e, pts) and all(x == [] for x in r_e))
    sc._reason_context = _orig_ctx
    sc._race_common = _boom
    p_e, r_e = sc.score_with_reasons(df, m, top_n=40)
    check("_race_common が落ちても点数は同じ・内訳は空", np.allclose(p_e, pts) and all(x == [] for x in r_e))
finally:
    sc._reason_context, sc._race_common = _orig_ctx, _orig_common
p_ok, _ = sc.score_with_reasons(df, m, top_n=40)
check("元に戻すと同じ点数", np.allclose(p_ok, pts))

# 7e. 古いキャッシュ (#169 以前) の内訳を export でそろえる
_k = "avg_pos_ratio=5/5"
_pt = round(m["pts"].get(_k, 0.0) * 10, 1)
old_h = [{"horse_number": 1, "sc_reasons": [
    {"label": "道中の位置取り", "bin": "5/5", "points": _pt},
    {"label": "休養日数", "bin": "4ヶ月以上(休み明け)", "points": 1.2},
    {"label": "騎手の複勝率", "bin": "1位", "points": 3.0}]},
         {"horse_number": 2, "sc_reasons": [
    {"label": "道中の位置取り", "bin": "5/5", "points": _pt + 2.0}]}]
import copy  # noqa: E402
h1 = copy.deepcopy(old_h)
tr, dr = sc.sanitize_cached_reasons(h1, "3歳以上1勝クラス", m)
check("点数が今の点数表と合う「5/5」は表示名 (後方) に訳す",
      h1[0]["sc_reasons"][0]["bin"] == "後方" and tr == 1, h1[0]["sc_reasons"])
check("点数が合わない「5/5」(別の版) は訳さず外す", h1[1]["sc_reasons"] == [] and dr == 1, h1[1])
check("新馬戦でなければ休養日数は残す", any(r["label"] == "休養日数" for r in h1[0]["sc_reasons"]))
h2 = copy.deepcopy(old_h)
sc.sanitize_cached_reasons(h2, "2歳新馬", m)
check("新馬戦は馬の過去走の項目 (休み明け・位置取り) を外し、騎手は残す",
      [r["label"] for r in h2[0]["sc_reasons"]] == ["騎手の複勝率"], h2[0]["sc_reasons"])
h3 = [{"horse_number": 1, "sc_reasons": [dict(r) for r in reasons[13]]}]
_before = copy.deepcopy(h3)
check("新しい内訳には何もしない", sc.sanitize_cached_reasons(h3, "3歳以上1勝クラス", m) == (0, 0)
      and h3 == _before)

if FAILS:
    print(f"❌ {len(FAILS)} 件失敗")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("✅ すべて通過")
