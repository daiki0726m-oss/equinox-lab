#!/usr/bin/env python3
"""画面・投稿に出す「スピード指数」を点数表の内訳と同じ値にそろえる (#169)。

## なぜ作ったか
v3 の点数表 (#168) は区分項目「平均スピード指数」「直近スピード指数」の値を
model["col_map"] で si_avg → si_avg_v (馬場差を引いた V2d) に読み替えている。
一方で画面の「SI」、馬行の「能」バッジ、Threads の「スピード指数N位」は旧指数
(LGB 用の si_avg) のままで、内訳に平均スピード指数が出ている印馬108頭のうち50頭で
区分 (1位 / 2-3位 / …) が画面の SI の順位と違っていた (2026-10-31・11-01)。

## 決まり
- predict は点数表が V2d を読んだレースだけ、各馬に si_avg_v / si_latest_v を書く
  (代用モード = col_map を外した時や点数表が無い時は書かない)。
- 表示側はキー si_avg_v が **ある時はそれ**、無い時は si_avg を出す (display_si)。
  = 「キーがある」が「内訳の指数は V2d」の印。値が 0 / None なら「データなし」(0 を出す)。
- 過去のキャッシュ (このキーが無い) は export 時に backfill_cached で補う。内訳の区分と
  V2d の順位が全件一致したレースにだけ書き、合わなければ (v2 以前の点数表など) 書かない。

LGB の特徴量・点数・印は一切変えない (表示だけ)。
"""
import math

SI_V_KEYS = ("si_avg_v", "si_latest_v")
SI_REASON_LABELS = {"平均スピード指数": "si_avg_v", "直近スピード指数": "si_latest_v"}
_REL_BINS = [(1, 1, "1位"), (2, 3, "2-3位"), (4, 6, "4-6位"), (7, 99, "7位以下")]   # ml/scorecard.REL_BINS


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def display_si(h, latest=False):
    """その馬の表示用スピード指数。点数表の内訳 (平均/直近スピード指数) と同じ値。"""
    k = "si_latest" if latest else "si_avg"
    if (k + "_v") in h:
        return _f(h.get(k + "_v"))
    return _f(h.get(k))


def si_rank(horses, horse, latest=False):
    """出走馬中の順位 (大きいほど上、同値は同順位 = 点数表の _rel_rank と同じ method=min)。
    ちょうど 0 (データなし) の馬は順位なし (None)。V2d は大敗した走で負になりうるが、
    点数表と同じく負の値も順位に入れる (データなし扱いは 0 だけ)。"""
    v = display_si(horse, latest)
    if v == 0:
        return None
    return 1 + sum(1 for h in horses if display_si(h, latest) > v)


def scorecard_si_cols(model):
    """点数表が平均・直近スピード指数の区分に読む列 (V2d なら si_avg_v / si_latest_v)。"""
    cm = (model or {}).get("col_map") or {}
    return cm.get("si_avg", "si_avg"), cm.get("si_latest", "si_latest")


def fields_from_row(row, model):
    """predict の1頭分 (pred_df の行) から、キャッシュに書く V2d の表示値。
    点数表が V2d を読んでいない (代用モード・点数表なし) なら空 = 書かない。"""
    a, l = scorecard_si_cols(model)
    if a != "si_avg_v" or l != "si_latest_v" or (model or {}).get("_degraded"):
        return {}
    if "si_avg_v" not in row or "si_latest_v" not in row:
        return {}
    return {"si_avg_v": round(_f(row.get("si_avg_v")), 1),
            "si_latest_v": round(_f(row.get("si_latest_v")), 1)}


def _bin_of(rank):
    if rank is None:
        return "データなし"
    for lo, hi, nm in _REL_BINS:
        if lo <= rank <= hi:
            return nm
    return "データなし"


def _rank_unrounded(vals, v):
    if v == 0:                       # 点数表 (_rel_rank, zero_na) と同じく 0 だけがデータなし
        return None
    return 1 + sum(1 for x in vals if x > v)


_DSV = {}
_DATE_TALLY = {}     # race_date -> [ok, mismatch]。v2 以前の日は2レース合わなければ以降を省く


def backfill_cached(conn_factory, race_id, race_date, horses, model=None):
    """V2d のキーが無い古いキャッシュ (#169 より前) の horses に si_avg_v / si_latest_v を補う。

    内訳の「平均/直近スピード指数」の区分が、補った V2d の順位と **全件** 一致した時だけ書く
    (= そのレースの内訳は確かに V2d で作られていた)。区分が1件も無い・1件でも合わない
    (v2 以前の点数表、代用モードで作ったキャッシュ等) なら何も書かない。
    戻り値: "skip" (補う必要なし) / "ok" (書いた) / "mismatch" / "noevidence" / "unavailable"。
    """
    if not horses or any(k in h for h in horses for k in SI_V_KEYS):
        return "skip"
    if model is None:
        from ml.scorecard import load_model
        model = load_model()
    if scorecard_si_cols(model) != ("si_avg_v", "si_latest_v"):
        return "unavailable"
    ev = []
    for h in horses:
        for r in h.get("sc_reasons") or []:
            col = SI_REASON_LABELS.get(r.get("label"))
            if col:
                ev.append((h.get("horse_number"), col, r.get("bin")))
    if not ev:
        return "noevidence"
    tally = _DATE_TALLY.setdefault(race_date, [0, 0])
    if tally[1] >= 2 and tally[0] == 0:
        return "mismatch"            # この日は V2d で作られていない (計算を省く。1日 約30秒かかる)
    from ml.scorecard import check_si_variant
    if check_si_variant(model):
        return "unavailable"
    from ml import speed_variant as sv
    table = sv.load_table()
    if table is None:
        return "unavailable"
    key = table.get("table_hash")
    if key not in _DSV:
        _DSV[key] = sv.DbSiVariant(table, conn_factory)
    dsv = _DSV[key]
    with conn_factory() as conn:
        hid = {r[0]: r[1] for r in conn.execute(
            "SELECT horse_number, horse_id FROM results WHERE race_id = ?", (race_id,))}
    vals = {}
    for h in horses:
        f = dsv.row_features(hid.get(h.get("horse_number")), race_date)
        vals[h.get("horse_number")] = (_f(f.get("si_avg_v")), _f(f.get("si_latest_v")))
    cols = {"si_avg_v": 0, "si_latest_v": 1}
    allv = {c: [v[i] for v in vals.values()] for c, i in cols.items()}
    for hn, col, b in ev:
        v = vals.get(hn, (0.0, 0.0))[cols[col]]
        if _bin_of(_rank_unrounded(allv[col], v)) != b:
            tally[1] += 1
            return "mismatch"
    tally[0] += 1
    for h in horses:
        a, l = vals.get(h.get("horse_number"), (0.0, 0.0))
        h["si_avg_v"] = round(a, 1)
        h["si_latest_v"] = round(l, 1)
    return "ok"


_SC_MODEL = []


def scorecard_for_display():
    """predict を通らない表示経路 (app.py のライブ予測) 用の点数表。predict._load_scorecard と
    同じく、V2d の基準表が使えなければ col_map を外した代用 (= SI は旧指数で出す)。"""
    if not _SC_MODEL:
        try:
            from ml.scorecard import load_model, check_si_variant, without_col_map
            m = load_model()
            msg = check_si_variant(m) if m else None
            _SC_MODEL.append(without_col_map(m, msg) if (m and msg) else m)
        except Exception:
            _SC_MODEL.append(None)
    return _SC_MODEL[0]
