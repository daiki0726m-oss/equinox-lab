#!/usr/bin/env python3
"""馬場差を引いたスピード指数 V2d と芝ダ替わりの項目 — 点数表 (ml/scorecard.py) 専用 (#168)。

## なぜ作ったか
現行のスピード指数 (analyzers/speed_index, fast_train._SpeedIndexCache) は
「場×距離×芝ダ×馬場状態(4段階)」の基準タイムとの差で、同じ「良」でも日によって
数秒ずれる馬場の速さ (日ごとの馬場差) を引いていない。V2d はその日・その場・その芝ダの
全平地レースの勝ち時計の水準から日ごとの馬場差 δ̂ を推定して引く。
LGB が使う si_avg / si_latest は一切変えない (再学習不要)。点数表だけが
新しい列 si_avg_v / si_latest_v を model["col_map"] 経由で読む。

## 定義 (学習側 fast_train と推論側 ml/features が **この1か所** を呼ぶ)
レース r の点   P_r = −距離係数 × (そのレースの1〜3着の走破時計の平均)
                   (距離係数は現行のスピード指数と同じ 15/12/10/8.5/7)
当てはめ        P_r = μ[場×芝ダ×距離] + κ[芝ダ×格×年齢区分×時期] + δ[日付×場×芝ダ]
                   を交互平均 (40回) で解く。窓 = 2020-01-01〜カットオフ日の非障害の JRA 平地
                   レース (1〜3着の時計がそろうもの)。μ̂・κ̂ を基準表に保存する。
芝ダの基準      交互平均の収束経路に任せず、κ̂ をレース数で重み付けして芝ダごとに平均0に
                   正規化する (同じだけ μ̂ に足すので当てはめた値は変わらない)。
                   その上でダートの走に足すずらし幅 c を明示的なパラメータにする
                   (scripts/build_si_variant.py が窓の中のデータだけで事前に決めた規則で選ぶ)。
日ごとの馬場差  δ̂[日] = その日・その場・その芝ダの全平地レースの残差 (P_r − μ̂ − κ̂) の平均
                   (そのレース自身も含む、縮小なし)。
                   **その日・その場のレース番号がちょうど 1..12 (JRA の1日・1場の通常の番組) で
                   DB にあり、かつその芝ダの平地レースが全部 1〜3着の時計を持つ時だけ** 使う。
                   (歯抜けだけでなく末尾のレースが DB に無い日も「そろっていない」。中止で
                   12レース未満になった日も、DB だけでは取り込み漏れと区別できないので使わない。)
走の V2d        80 + (P_走 − μ̂_コース) − δ̂_日 (+ ダートの走なら c)
                   P_走 = −距離係数 × その馬の走破時計。
                   残差が作れない走 (障害・地方・μ̂/κ̂ が3レース未満・2020年より前・
                   そろっていない日) は **現行のスピード指数** で埋める。
行の値          予想日より前の走 (走破タイム>0) の新しい順に最大5走 (現行の si_avg / si_latest と
                   同じ集め方) から si_avg_v (平均) / si_latest_v (直近) / si_v_nfb (現行で埋めた走数)。

基準表には μ̂・κ̂・c と一緒に **その時のコードの定義** (definition(): 距離係数・時期区分・格の対応・
行の窓・JRA の場・1日の番組数・規則の版・年齢区分と障害の判定のプローブ) を書き、load_table が
今のコードと照合する。違えば例外 (= 基準表と点数表の作り直しが必要。黙って別物の V2d を作らない)。

年齢区分は「レース名に 2歳 / 3歳 / 3歳以上(4歳以上) があればそれ、読めない時だけ出走馬の
horses.birth_year (最若年<=2 → 2歳戦、最年長<=3 → 3歳限定、それ以外 → 古馬)」。
時期区分は 2歳戦 = 〜8月 a / 〜10月 b / それ以降 c、3歳限定 = 〜3月 a / 〜6月 b / それ以降 c。

## 芝ダ替わりの項目 (surface_switch_features)
点数表は「芝→初ダート」の馬の3着内を実際の1.4倍に見積もる (実際/予測 0.72)。原因は
今回と違う芝ダの走から作った近走の項目がそのまま加点されること。候補 (事前に決めた3つ):
  sf_first_switch  過去の確定平地走が1走以上あり、今回の芝ダの走が0 (=初ダート/初芝) なら 1
  sf_other_share5  近5走 (確定平地走) のうち今回と違う芝ダの走の割合 (0〜1)
どちらも race_date より前の確定平地走だけから作る。今回が障害・芝ダ不明・過去走なしは 0。
"""
import hashlib
import json
import math
import os
import time

import numpy as np
import pandas as pd

from race_utils import is_jump_race

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLE_PATH = os.path.join(ROOT, "models", "si_variant_table.json")
TABLE_VERSION = "si_variant_v1"

JRA_VENUES = ("札幌", "函館", "福島", "新潟", "東京", "中山", "中京", "京都", "阪神", "小倉")
SURFACES = ("芝", "ダート")
DIST_EDGES = (1200, 1600, 2000, 2500)          # 距離係数の区切り (現行のスピード指数と同じ)
DIST_FACTORS = (15.0, 12.0, 10.0, 8.5, 7.0)
FIT_FROM = "2020-01-01"                         # 基準表の窓の初日 (= V2d を作る走の初日)
FIT_ITERS = 40
MIN_COURSE_N = 3                                 # μ̂ を使うのに必要な当てはめ窓のレース数
MIN_CLS_N = 3                                    # κ̂ を使うのに必要な当てはめ窓のレース数
WINDOW = 5                                       # 行の値に使う走の数 (現行 si_avg と同じ)
C_GRID = tuple(range(-10, 31, 2))                # ダートのずらし幅 c の候補 (−10〜+30、2刻み)
# 年齢区分ごとの時期の区切り (月の上限, 記号)
SEASON_DEF = {"2": ((8, "a"), (10, "b"), (12, "c")), "3": ((3, "a"), (6, "b"), (12, "c")), "古": ()}
GRADE_MAP = {"": "OP", "１勝クラス": "1勝", "２勝クラス": "2勝", "３勝クラス": "3勝"}
AGE_RULES = ("name_then_birth", "birth")          # birth = 検証時の版 (出走馬の生年だけ)

FULL_CARD = 12                                   # JRA の1日・1場の通常のレース数 (そろっているかの判定)

SV_COLS = ("si_avg_v", "si_latest_v", "si_v_nfb")
SF_COLS = ("sf_first_switch", "sf_other_share5")

# ── 定義の版 (基準表に記録し、読み込み時にコードと照合する) ─────────────────
# μ̂・κ̂・c は下の定義 (距離係数・時期区分・格の対応・年齢区分の名前規則・障害の判定・
# JRA の場・そろっているかの規則…) の上で当てはめた値。コード側の定義だけ変わると
# 基準表も点数表も作り直さないまま V2d が黙って別物になる (#168 レビュー)。
# 関数の規則 (age_group_from_name / race_utils.is_jump_race / race_table のそろっているかの
# 判定) を変えたら、対応する RULE_VERSIONS の文字列を上げること。さらに代表的な名前での
# 出力 (プローブ) も記録・照合するので、版を上げ忘れても典型的な変更は検出される。
RULE_VERSIONS = {
    "age_from_name": "name_2_3over_3:v1",          # 2歳 → 3歳以上/4歳以上 → 3歳 の順に読む
    "jump": "race_utils.is_jump_race:v163",         # #163 (名前が J で終わる障害重賞) 以降の規則
    "complete": "full_card_1to12+all_flat_t3:v2",   # #168 レビュー: 末尾欠けも検出 (v1 は 1..最大 の歯抜けだけ)
    "flat": "jra_venue+turf_dirt+dist>0+not_jump:v1",
}
AGE_PROBES = ("2歳新馬", "2歳未勝利", "2歳1勝クラス", "東京スポーツ杯2歳S(GII)", "3歳未勝利", "3歳1勝クラス",
              "3歳以上1勝クラス", "3歳以上2勝クラス", "4歳以上2勝クラス", "4歳以上3勝クラス",
              "障害3歳以上未勝利", "天皇賞(秋)(GI)", "有馬記念", "テストS", "")
JUMP_PROBES = ("障害3歳以上未勝利", "障害4歳以上オープン", "中山大障害(J・GI)", "中山グランドジャンプ",
               "中山グランドJ", "阪神スプリングJ", "東京HJ", "京都ハイJ", "新潟JS", "東京JS",
               "イルミネーションJS", "YJSトライアル", "ヤングジョッキーズシリーズ", "3歳以上1勝クラス",
               "2歳新馬", "スプリンターズS", "有馬記念", "")


# ══════════════════════════════════════════════════════════════════════
# 部品
# ══════════════════════════════════════════════════════════════════════
def dist_factor(d):
    """距離係数 (1秒あたりの点)。現行のスピード指数と同じ区分。"""
    d = np.asarray(d, dtype=float)
    return np.select([d < DIST_EDGES[0], d < DIST_EDGES[1], d < DIST_EDGES[2], d < DIST_EDGES[3]],
                     list(DIST_FACTORS[:4]), DIST_FACTORS[4])


def age_group_from_name(name):
    """レース名から年齢区分を読む。'2' / '3' / '古' / None (読めない)。
    '3歳以上' '4歳以上' は古馬戦 ('3歳' より先に判定する)。"""
    n = name if isinstance(name, str) else ""
    if "2歳" in n:
        return "2"
    if "3歳以上" in n or "4歳以上" in n:
        return "古"
    if "3歳" in n:
        return "3"
    return None


def _season(agegrp, month):
    for upper, sym in SEASON_DEF.get(agegrp, ()):
        if month <= upper:
            return sym
    return ""


def _canon(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def definition():
    """基準表を作った時のコード側の定義 (定数・規則の版・代表的な名前での出力)。
    make_table が表に書き、load_table が今のコードと照合する。"""
    return {
        "dist_factors": {"edges": list(DIST_EDGES), "factors": list(DIST_FACTORS)},
        "season_def": {k: [list(x) for x in v] for k, v in SEASON_DEF.items()},
        "grade_map": dict(GRADE_MAP),
        "row_window": WINDOW,
        "jra_venues": list(JRA_VENUES),
        "surfaces": list(SURFACES),
        "full_card": FULL_CARD,
        "fit_iters": FIT_ITERS,
        "rule_versions": dict(RULE_VERSIONS),
        "probes": {
            "age_from_name": [[n, age_group_from_name(n)] for n in AGE_PROBES],
            "is_jump": [[n, bool(is_jump_race(n))] for n in JUMP_PROBES],
        },
    }


DEF_KEYS = tuple(definition().keys())


def definition_hash(defn=None):
    return hashlib.sha256(_canon(defn if defn is not None else definition()).encode("utf-8")).hexdigest()[:16]


def definition_diff(table):
    """表に記録された定義と今のコードの定義が違うキーのリスト (表に定義が無ければ全部)。"""
    cur = definition()
    return [k for k in DEF_KEYS if _canon(table.get(k)) != _canon(cur[k])]


def table_hash(table):
    """基準表の中身 (μ̂・κ̂・件数・c・規則・定義の指紋) の指紋。点数表 (pkl) に記録し、食い違いを検知する。"""
    keys = ("version", "cutoff", "fit_from", "mu", "n_course", "kap", "n_cls", "dirt_shift_c",
            "age_rule", "require_complete", "min_course_n", "min_cls_n", "dist_factors",
            "definition_hash")
    body = {k: table.get(k) for k in keys}
    return hashlib.sha256(_canon(body).encode("utf-8")).hexdigest()[:16]


# ══════════════════════════════════════════════════════════════════════
# レース表 (学習側は全レース、推論側はその日・その場のレースだけを渡す。同じ関数)
# ══════════════════════════════════════════════════════════════════════
def race_table(races, results, age_rule="name_then_birth", apply_from=FIT_FROM):
    """レースごとの材料 (P3, コース, クラス, 日, そろっているか) を作る。

    races  : race_id, race_date, venue, surface, distance, grade, race_name, race_number
    results: race_id, horse_number, finish_position, finish_time_seconds, birth_year (出走馬の生年)
             = 渡したレースの **全出走馬の行** (着順0も含む。年齢区分の生年に使う)
    戻り値: レースごとの DataFrame (race_id 順)。
      flat      : JRA の芝/ダートの非障害で距離>0、race_date >= apply_from
      has_t3    : 1〜3着の走破時計が3頭以上あり、1着の時計がある
      complete  : その日・その場の番号が歯抜けなし & その芝ダの平地レースが全部 has_t3
    """
    if age_rule not in AGE_RULES:
        raise ValueError(f"age_rule は {AGE_RULES} のどれか: {age_rule}")
    ra = races[["race_id", "race_date", "venue", "surface", "distance", "grade",
                "race_name", "race_number"]].copy()
    ra["race_date"] = ra["race_date"].fillna("").astype(str)
    ra["venue"] = ra["venue"].fillna("").astype(str)
    ra["surface"] = ra["surface"].fillna("").astype(str)
    ra["race_name"] = ra["race_name"].fillna("").astype(str)
    ra["grade"] = ra["grade"].fillna("").astype(str)
    ra["distance"] = pd.to_numeric(ra["distance"], errors="coerce").fillna(0).astype(int)
    ra["race_number"] = pd.to_numeric(ra["race_number"], errors="coerce").fillna(0).astype(int)
    ra = ra.drop_duplicates("race_id").sort_values("race_id", kind="mergesort").reset_index(drop=True)
    ra["is_jump"] = ra["race_name"].map(is_jump_race).astype(bool)
    ra["flat"] = (ra["venue"].isin(JRA_VENUES) & ra["surface"].isin(SURFACES)
                  & (ra["distance"] > 0) & ~ra["is_jump"] & (ra["race_date"] >= apply_from))

    rs = results[["race_id", "horse_number", "finish_position", "finish_time_seconds",
                  "birth_year"]].copy()
    rs = rs[rs["race_id"].isin(ra["race_id"])]
    rs["fp"] = pd.to_numeric(rs["finish_position"], errors="coerce").fillna(0)
    rs["t"] = pd.to_numeric(rs["finish_time_seconds"], errors="coerce").fillna(0.0).astype(float)
    rs["hn"] = pd.to_numeric(rs["horse_number"], errors="coerce").fillna(0)
    rs = rs.sort_values(["race_id", "hn"], kind="mergesort")
    ok = rs[(rs["t"] > 0) & (rs["fp"] > 0)]
    top3 = ok[ok["fp"].between(1, 3)].groupby("race_id", sort=True)["t"].agg(["mean", "size"])
    t1 = ok[ok["fp"] == 1].groupby("race_id", sort=True)["t"].min()
    ra["t3"] = ra["race_id"].map(top3["mean"])
    ra["n3"] = ra["race_id"].map(top3["size"]).fillna(0).astype(int)
    ra["t1"] = ra["race_id"].map(t1)
    ra["has_t3"] = ra["t3"].notna() & (ra["n3"] >= 3) & ra["t1"].notna()

    # 年齢区分: レース名 → 読めなければ出走馬の生年 (最若年/最年長)
    yr = pd.to_numeric(ra["race_date"].str[:4], errors="coerce")
    by = pd.to_numeric(rs["birth_year"], errors="coerce")
    age = rs["race_id"].map(pd.Series(yr.values, index=ra["race_id"].values)) - by
    agg = age.groupby(rs["race_id"].values).agg(["min", "max"])
    amin = ra["race_id"].map(agg["min"]).to_numpy(dtype=float)
    amax = ra["race_id"].map(agg["max"]).to_numpy(dtype=float)
    with np.errstate(invalid="ignore"):
        ag_birth = np.where(amin <= 2, "2", np.where(amax <= 3, "3", "古"))   # NaN → 古 (検証時と同じ)
    if age_rule == "birth":
        agegrp = ag_birth
    else:
        nm = ra["race_name"].map(age_group_from_name)
        agegrp = np.where(nm.notna(), nm.fillna("").to_numpy(dtype=object), ag_birth)
    ra["agegrp"] = agegrp
    month = pd.to_numeric(ra["race_date"].str[5:7], errors="coerce").fillna(0).astype(int)
    ra["season"] = [_season(a, m) for a, m in zip(ra["agegrp"], month)]
    g = ra["grade"].replace(GRADE_MAP)
    ra["cls"] = ra["surface"] + "|" + g + "|" + ra["agegrp"] + ra["season"]
    ra["course"] = ra["venue"] + "|" + ra["surface"] + "|" + ra["distance"].astype(str)
    ra["day"] = ra["race_date"] + "|" + ra["venue"] + "|" + ra["surface"]
    ra["P3"] = -dist_factor(ra["distance"]) * ra["t3"]

    # そろっているか: (1) その日・その場のレース番号がちょうど 1..FULL_CARD (障害・他の芝ダも含めた全レース)
    #                     — 歯抜けだけでなく末尾のレースが DB に無い日も検出する (#168 レビュー:
    #                     旧規則 1..最大 は 2020-03-29 中山 (1-2R のみ) 等を「そろった」と誤判定した)
    #                 (2) その日・その場・その芝ダの平地レースが全部 has_t3
    dv = ra["race_date"] + "|" + ra["venue"]
    full = set(range(1, FULL_CARD + 1))
    gap_ok = {}
    for k, s in ra["race_number"].groupby(dv.values):
        v = [int(x) for x in s]
        gap_ok[k] = len(v) == FULL_CARD and set(v) == full
    miss = (ra["flat"] & ~ra["has_t3"]).groupby(ra["day"].values).any()
    ra["complete"] = dv.map(gap_ok).fillna(False).astype(bool) & ~ra["day"].map(miss).fillna(False).astype(bool)
    return ra


# ══════════════════════════════════════════════════════════════════════
# 基準表 (μ̂・κ̂) の当てはめ
# ══════════════════════════════════════════════════════════════════════
def fit_table(rt, cutoff, fit_from=FIT_FROM, iters=FIT_ITERS, normalize_surface=True):
    """[fit_from, cutoff] の flat & has_t3 のレースで P3 = μ[course] + κ[cls] + δ[day] を交互平均。
    戻り値 dict: mu / kap (pd.Series)、n_course / n_cls、kap_shift (正規化で引いた芝ダごとの量)。"""
    fit = rt[rt["flat"] & rt["has_t3"] & (rt["race_date"] >= fit_from)
             & (rt["race_date"] <= cutoff)].reset_index(drop=True)
    if fit.empty:
        raise ValueError(f"当てはめ窓 {fit_from}〜{cutoff} にレースが無い")
    y = fit["P3"].to_numpy(dtype=float)
    cv, kv, dv = fit["course"].values, fit["cls"].values, fit["day"].values
    mu = fit.groupby("course")["P3"].transform("mean").to_numpy().copy()
    kap = np.zeros(len(fit))
    dl = np.zeros(len(fit))
    for _ in range(iters):
        dl = pd.Series(y - mu - kap).groupby(dv).transform("mean").to_numpy().copy()
        dl -= dl.mean()
        kap = pd.Series(y - mu - dl).groupby(kv).transform("mean").to_numpy().copy()
        kap -= kap.mean()
        mu = pd.Series(y - kap - dl).groupby(cv).transform("mean").to_numpy().copy()
    mu_map = pd.Series(mu, index=cv).groupby(level=0).first()
    kap_map = pd.Series(kap, index=kv).groupby(level=0).first()
    shift = {}
    if normalize_surface:
        # κ̂ をレース数で重み付けして芝ダごとに平均0にする (同じ量を μ̂ に足す = 当てはめ値は不変)
        a = pd.Series(kap).groupby(fit["surface"].values).mean()
        shift = {str(k): float(v) for k, v in a.items()}
        ks = kap_map.index.str.split("|").str[0]
        cs = mu_map.index.str.split("|").str[1]
        kap_map = kap_map - np.array([shift.get(s, 0.0) for s in ks])
        mu_map = mu_map + np.array([shift.get(s, 0.0) for s in cs])
    resid = y - mu - kap - dl
    return {"mu": mu_map, "kap": kap_map,
            "n_course": fit.groupby("course").size(), "n_cls": fit.groupby("cls").size(),
            "kap_shift": shift, "fit_races": int(len(fit)),
            "fit_first_date": str(fit["race_date"].min()), "fit_last_date": str(fit["race_date"].max()),
            "resid_sd": float(np.std(resid)),
            "kap_surface_mean_after": {str(k): float(v) for k, v in pd.Series(
                kap - np.array([shift.get(s, 0.0) for s in fit["surface"].values]))
                .groupby(fit["surface"].values).mean().items()}}


def make_table(fitd, cutoff, c=0.0, age_rule="name_then_birth", require_complete=True,
               fit_from=FIT_FROM, extra=None):
    """当てはめ結果を JSON に書ける基準表にする。"""
    t = {
        "version": TABLE_VERSION,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cutoff": cutoff,
        "fit_from": fit_from,
        "window": f"{fit_from}〜{cutoff} の非障害の JRA 平地レース (1〜3着の時計がそろうもの)",
        "fit_races": fitd["fit_races"],
        "fit_first_date": fitd["fit_first_date"], "fit_last_date": fitd["fit_last_date"],
        **definition(),                 # 定数・規則の版・プローブ (load_table が今のコードと照合する)
        "definition_hash": definition_hash(),
        "age_rule": age_rule,
        "require_complete": bool(require_complete),
        "min_course_n": MIN_COURSE_N, "min_cls_n": MIN_CLS_N,
        "dirt_shift_c": float(c),
        "kap_surface_shift_applied": fitd.get("kap_shift", {}),
        "kap_surface_mean_after": fitd.get("kap_surface_mean_after", {}),
        "resid_sd": fitd.get("resid_sd"),
        "mu": {str(k): float(v) for k, v in fitd["mu"].items()},
        "n_course": {str(k): int(v) for k, v in fitd["n_course"].items()},
        "kap": {str(k): float(v) for k, v in fitd["kap"].items()},
        "n_cls": {str(k): int(v) for k, v in fitd["n_cls"].items()},
    }
    if extra:
        t.update(extra)
    t["table_hash"] = table_hash(t)
    return t


_TABLE_CACHE = {}


def table_path(path=None):
    """基準表のパス。既定は models/si_variant_table.json。環境変数 SI_VARIANT_TABLE_PATH は
    **検証専用** (DB のコピーと出荷前の基準表で照合する時)。本番の workflow では設定しないこと
    (#150: env でしか効かない設定は経路ごとに書き忘れる)。"""
    return path or os.environ.get("SI_VARIANT_TABLE_PATH") or TABLE_PATH


def load_table(path=None):
    """基準表を読む (無ければ None)。mtime でキャッシュ。次のどれかなら例外 (黙って使わない):
    版が違う / 指紋が中身と合わない / 表を作った時の定義 (距離係数・時期区分・格の対応・
    行の窓・JRA の場・そろっているかの規則・年齢区分と障害の判定の版とプローブ) が今のコードと違う。
    定義のキャッシュ判定はコードの定義の指紋も見る (プロセス内で定数を書き換えても素通りしない)。"""
    p = table_path(path)
    if not os.path.exists(p):
        return None
    mt = os.path.getmtime(p)
    dh = definition_hash()
    hit = _TABLE_CACHE.get(p)
    if hit and hit[0] == mt and hit[2] == dh:
        return hit[1]
    with open(p, encoding="utf-8") as f:
        t = json.load(f)
    if t.get("version") != TABLE_VERSION:
        raise ValueError(f"{p}: 基準表の版 {t.get('version')} はコードの {TABLE_VERSION} と違う")
    if "definition_hash" not in t:
        raise ValueError(f"{p}: 基準表を作った時の定義の記録が無い (古い形式) — 基準表と点数表を作り直すこと "
                         f"(scripts/build_si_variant.py → train_scorecard --mode f2)")
    if t.get("table_hash") != table_hash(t):
        raise ValueError(f"{p}: 基準表の指紋が中身と合わない (手で書き換えた?)")
    diff = definition_diff(t)
    if diff or t.get("definition_hash") != dh:
        raise ValueError(f"{p}: 基準表を作った時の定義が今のコードと違う ({diff or ['definition_hash']}) "
                         f"— 基準表と点数表を作り直すこと (scripts/build_si_variant.py → train_scorecard --mode f2)")
    _TABLE_CACHE[p] = (mt, t, dh)
    return t


# ══════════════════════════════════════════════════════════════════════
# 日ごとの馬場差と走の V2d
# ══════════════════════════════════════════════════════════════════════
def race_residuals(rt, table):
    """レースごとの残差 P_r − μ̂ − κ̂ (μ̂/κ̂ が3レース未満・flat でない・has_t3 でないなら NaN)。"""
    mu, kap = table["mu"], table["kap"]
    ncrs, ncls = table["n_course"], table["n_cls"]
    mn_c, mn_k = int(table.get("min_course_n", MIN_COURSE_N)), int(table.get("min_cls_n", MIN_CLS_N))
    okc = np.array([ncrs.get(c, 0) >= mn_c for c in rt["course"]], dtype=bool)
    okk = np.array([ncls.get(k, 0) >= mn_k for k in rt["cls"]], dtype=bool)
    m = np.array([mu.get(c, np.nan) for c in rt["course"]], dtype=float)
    k = np.array([kap.get(x, np.nan) for x in rt["cls"]], dtype=float)
    ok = rt["flat"].to_numpy(bool) & rt["has_t3"].to_numpy(bool) & okc & okk
    return np.where(ok, rt["P3"].to_numpy(dtype=float) - m - k, np.nan)


def day_offsets(rt, table, res=None):
    """日 (日付|場|芝ダ) → δ̂ (その日の残差の平均、縮小なし)。そろっていない日は入れない。"""
    if res is None:
        res = race_residuals(rt, table)
    req = bool(table.get("require_complete", True))
    d = pd.DataFrame({"day": rt["day"].values, "res": res, "complete": rt["complete"].values})
    d = d[np.isfinite(d["res"].to_numpy())]
    if req:
        d = d[d["complete"]]
    return d.groupby("day", sort=True)["res"].mean().to_dict()


def run_values(runs, rt, table, doff=None, res=None, c=None):
    """走ごとの V2d (作れない走は NaN) とダートか (0/1)。

    runs: race_id, finish_time_seconds (>0) を持つ DataFrame (行の順は保つ)。
    c: ダートのずらし幅 (既定は基準表の dirt_shift_c)。"""
    if res is None:
        res = race_residuals(rt, table)
    if doff is None:
        doff = day_offsets(rt, table, res)
    c = float(table.get("dirt_shift_c", 0.0)) if c is None else float(c)
    pos = pd.Series(np.arange(len(rt)), index=rt["race_id"].values)
    ix = runs["race_id"].map(pos)
    has = ix.notna().to_numpy()
    ii = ix.fillna(0).astype(int).to_numpy()
    t = pd.to_numeric(runs["finish_time_seconds"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    course = rt["course"].to_numpy(dtype=object)[ii]
    day = rt["day"].to_numpy(dtype=object)[ii]
    dist = rt["distance"].to_numpy(dtype=float)[ii]
    surf = rt["surface"].to_numpy(dtype=object)[ii]
    rr = np.where(has, res[ii], np.nan)
    mn_c = int(table.get("min_course_n", MIN_COURSE_N))
    okc = np.array([table["n_course"].get(cc, 0) >= mn_c for cc in course], dtype=bool)
    mu = np.array([table["mu"].get(cc, np.nan) for cc in course], dtype=float)
    dd = np.array([doff.get(x, np.nan) for x in day], dtype=float)
    dirt = has & (surf == "ダート")
    ok = has & okc & np.isfinite(rr) & np.isfinite(dd) & (t > 0)
    v = ((80.0 - dist_factor(dist) * t) - mu) - dd + c * dirt.astype(float)
    return np.where(ok, v, np.nan), dirt & ok


def build_run_lookup(races_df, results_df, table, c=None, with_dirt=False):
    """学習側: 全レース・全走から {(horse_id, race_id): V2d} を作る (V2d が作れた走だけ)。
    with_dirt=True なら {(horse_id, race_id): (V2d, ダートでずらした走か)}。"""
    rt = race_table(races_df, results_df, age_rule=table.get("age_rule", "name_then_birth"),
                    apply_from=table.get("fit_from", FIT_FROM))
    res = race_residuals(rt, table)
    doff = day_offsets(rt, table, res)
    runs = results_df[["race_id", "horse_id", "finish_time_seconds"]]
    runs = runs[pd.to_numeric(runs["finish_time_seconds"], errors="coerce").fillna(0) > 0]
    v, dirt = run_values(runs, rt, table, doff, res, c=c)
    ok = np.isfinite(v)
    keys = zip(runs["horse_id"].values[ok], runs["race_id"].values[ok])
    if with_dirt:
        return {k: (float(a), bool(b)) for k, a, b in zip(keys, v[ok], dirt[ok])}
    return {k: float(a) for k, a in zip(keys, v[ok])}


def aggregate(values, n_fallback=0):
    """行の値: 新しい順の最大5走の値から 平均 / 直近。走が無ければ 0 (現行 si_avg と同じ)。"""
    if not values:
        return {"si_avg_v": 0.0, "si_latest_v": 0.0, "si_v_nfb": 0}
    return {"si_avg_v": float(np.mean(values)), "si_latest_v": float(values[0]),
            "si_v_nfb": int(n_fallback)}


def row_components(horse_id, si_runs, lookup, fallback):
    """si_runs (予想日より前・走破タイム>0・新しい順に最大5走、各要素は race_id を持つ) の値の列。
    lookup に V2d があればそれ、無ければ fallback(run) = 現行のスピード指数。
    戻り値: [(値, V2d を使ったか)]"""
    out = []
    for s in si_runs:
        v = lookup.get((horse_id, s.get("race_id")))
        if v is None:
            out.append((fallback(s), False))
        else:
            out.append((v, True))
    return out


def row_features(horse_id, si_runs, lookup, fallback):
    comp = row_components(horse_id, si_runs, lookup, fallback)
    return aggregate([v for v, _ in comp], sum(1 for _, u in comp if not u))


class TrainSiVariant:
    """学習側 (fast_train) 用: 全走の V2d を前計算して持つ。"""

    def __init__(self, races_df, results_df, table):
        self.table = table
        self.lookup = build_run_lookup(races_df, results_df, table)

    def row_features(self, horse_id, si_runs, fallback):
        return row_features(horse_id, si_runs, self.lookup, fallback)


# ══════════════════════════════════════════════════════════════════════
# 芝ダ替わり
# ══════════════════════════════════════════════════════════════════════
def surface_switch_features(past_runs, cur_surface, cur_is_jump=False, window=WINDOW):
    """past_runs: race_date より前の確定走 (着順>0) を **新しい順** に。各要素は surface / is_jump。
    戻り値 {sf_first_switch, sf_other_share5}。今回が障害・芝ダ不明・確定平地走なしは 0。"""
    zero = {"sf_first_switch": 0.0, "sf_other_share5": 0.0}
    if cur_is_jump or cur_surface not in SURFACES:
        return zero
    flat = [r for r in (past_runs or [])
            if not r.get("is_jump") and r.get("surface") in SURFACES]
    if not flat:
        return zero
    first = 0.0 if any(r.get("surface") == cur_surface for r in flat) else 1.0
    last = flat[:window]
    share = sum(1 for r in last if r.get("surface") != cur_surface) / len(last)
    return {"sf_first_switch": first, "sf_other_share5": float(share)}


# ══════════════════════════════════════════════════════════════════════
# 推論側 (DB から必要な日だけ)
# ══════════════════════════════════════════════════════════════════════
TRACK_ADJ = {"良": 0, "稍重": -1.5, "重": -3.0, "不良": -5.0}


def legacy_dist_factor(d):
    """fast_train._SpeedIndexCache._dist_factor と同じ。"""
    if d < 1200:
        return 15.0
    elif d < 1600:
        return 12.0
    elif d < 2000:
        return 10.0
    elif d < 2500:
        return 8.5
    return 7.0


def legacy_si(bt, t, distance, tc):
    """現行のスピード指数 1走分 (fast_train._SpeedIndexCache.si と同じ式・同じ丸め)。"""
    if not bt or bt <= 0:
        return 0
    v = 80 + (bt - t) * legacy_dist_factor(distance) + TRACK_ADJ.get(tc, 0)
    return round(v, 1)


class DbLegacySpeedIndex:
    """推論側で V2d を作れない走を埋める現行のスピード指数。

    analyzers/speed_index は基準タイムを SQL の AVG で作るため、学習側 (累積和の平均) と
    round(…,1) の境界で 0.1 ずれる走がある (verify_feature_parity の SI_ROUND_TOL)。
    点数表の列は厳密一致が必要なので、ここでは学習側と **同じ順 (race_date, race_id, 馬番)・
    同じ累積和** で基準タイムを作る。規則 (件数の閾値・フォールバック) も学習側と同じ。"""

    def __init__(self, conn_factory):
        self._conn = conn_factory
        self._bt = {}

    def _avg(self, where, params, before):
        with self._conn() as conn:
            rows = conn.execute(f"""
                SELECT r.finish_time_seconds FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE {where} AND r.finish_time_seconds > 0
                  AND r.finish_position BETWEEN 1 AND 5 AND ra.race_date < ?
                ORDER BY ra.race_date, r.race_id, r.horse_number""", tuple(params) + (before,)).fetchall()
        n = len(rows)
        if not n:
            return None, 0
        cs = np.cumsum(np.asarray([float(r[0]) for r in rows], dtype=float))
        return cs[-1] / n, n

    def base_time(self, venue, distance, surface, tc, before):
        ck = (venue, distance, surface, tc, before)
        if ck in self._bt:
            return self._bt[ck]
        avg, n = self._avg("ra.venue = ? AND ra.distance = ? AND ra.surface = ? AND ra.track_condition = ?",
                           (venue, distance, surface, tc), before)
        if avg is not None and n >= 5:
            bt = avg
        else:
            avg2, n2 = self._avg("ra.venue = ? AND ra.distance = ? AND ra.surface = ?",
                                 (venue, distance, surface), before)
            if avg2 is not None and n2 >= 3:
                bt = avg2
            else:
                avg3, _ = self._avg("ra.distance = ? AND ra.surface = ?", (distance, surface), before)
                bt = avg3 if avg3 else distance * 0.06
        self._bt[ck] = bt
        return bt

    def si(self, run, before):
        bt = self.base_time(run["venue"], run["distance"], run["surface"], run["tc"], before)
        return legacy_si(bt, run["t"], run["distance"], run["tc"])


class DbSiVariant:
    """推論側 (ml/features.FeatureBuilder) 用: 必要な日 (日付×場) のレースだけを DB から引いて、
    学習側と同じ race_table / race_residuals / day_offsets / run_values で V2d を作る。"""

    def __init__(self, table, conn_factory):
        self.table = table
        self._conn = conn_factory
        self._days = {}            # (date, venue) -> {(horse_id, race_id): V2d}
        self.legacy = DbLegacySpeedIndex(conn_factory)

    def _day_lookup(self, race_date, venue):
        key = (race_date, venue)
        if key in self._days:
            return self._days[key]
        with self._conn() as conn:
            races = pd.DataFrame([dict(r) for r in conn.execute(
                "SELECT race_id, race_date, venue, surface, distance, grade, race_name, race_number "
                "FROM races WHERE race_date = ? AND venue = ?", (race_date, venue))],
                columns=["race_id", "race_date", "venue", "surface", "distance", "grade",
                         "race_name", "race_number"])
            ids = races["race_id"].tolist()
            rows = []
            if ids:
                q = ",".join("?" * len(ids))
                rows = [dict(r) for r in conn.execute(
                    f"SELECT r.race_id, r.horse_id, r.horse_number, r.finish_position, "
                    f"r.finish_time_seconds, h.birth_year FROM results r "
                    f"LEFT JOIN horses h ON r.horse_id = h.horse_id WHERE r.race_id IN ({q})", ids)]
        results = pd.DataFrame(rows, columns=["race_id", "horse_id", "horse_number", "finish_position",
                                              "finish_time_seconds", "birth_year"])
        out = {}
        complete = False
        if len(races):
            rt = race_table(races, results, age_rule=self.table.get("age_rule", "name_then_birth"),
                            apply_from=self.table.get("fit_from", FIT_FROM))
            res = race_residuals(rt, self.table)
            doff = day_offsets(rt, self.table, res)
            runs = results[pd.to_numeric(results["finish_time_seconds"], errors="coerce").fillna(0) > 0]
            if len(runs):
                v, _ = run_values(runs, rt, self.table, doff, res)
                ok = np.isfinite(v)
                out = {(h, r): float(x) for h, r, x in zip(runs["horse_id"].values[ok],
                                                           runs["race_id"].values[ok], v[ok])}
            complete = bool(rt.loc[rt["flat"], "complete"].all()) if rt["flat"].any() else True
        # 結果の取り込み途中の日 (そろっていない) は次回に作り直す (キャッシュしない)
        if complete:
            self._days[key] = out
        return out

    def row_features(self, horse_id, race_date):
        """予想日より前の走 (走破タイム>0、新しい順に最大5走) から si_avg_v / si_latest_v / si_v_nfb。
        集め方は analyzers/speed_index.get_horse_indices と同じ。race_date が無ければ過去走なし。"""
        if not horse_id or not race_date:
            return aggregate([])
        with self._conn() as conn:
            rows = [dict(r) for r in conn.execute("""
                SELECT r.race_id, r.finish_time_seconds AS t, ra.race_date, ra.venue, ra.distance,
                       ra.surface, ra.track_condition
                FROM results r JOIN races ra ON r.race_id = ra.race_id
                WHERE r.horse_id = ? AND r.finish_time_seconds > 0 AND ra.race_date < ?
                ORDER BY ra.race_date DESC LIMIT ?""", (horse_id, race_date, WINDOW))]
        si_runs = [{"race_id": r["race_id"], "t": r["t"], "race_date": r["race_date"],
                    "venue": r["venue"], "distance": r["distance"], "surface": r["surface"],
                    "tc": r["track_condition"] or "良"} for r in rows]
        lookup = {}
        for s in si_runs:
            lookup.update(self._day_lookup(s["race_date"], s["venue"]))
        return row_features(horse_id, si_runs, lookup, lambda s: self.legacy.si(s, race_date))


def fallback_share(df):
    """特徴量テーブル (si_v_nfb と si_count を持つ) で、現行の指数で埋めた走の割合。"""
    if "si_v_nfb" not in df.columns or "si_count" not in df.columns:
        return None
    n = pd.to_numeric(df["si_count"], errors="coerce").fillna(0).sum()
    k = pd.to_numeric(df["si_v_nfb"], errors="coerce").fillna(0).sum()
    return float(k / n) if n else None


def isfinite(x):
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False
