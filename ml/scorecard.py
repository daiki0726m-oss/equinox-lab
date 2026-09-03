#!/usr/bin/env python3
"""説明できる予想 — 加算式の点数表 (#145)。

## なぜこれを作ったか
現行モデルの評価は市場人気の写しだった (評価順位と人気順位の相関 0.90、
特徴量重要度は odds_log 68.5% + popularity_norm 16.8% = 85.2%)。
「なぜこの馬が本命か」を画面に出そうとしても、正直な答えは「人気だから」で、
UI に出していた6項目 (能力/血統/騎手/…) は人気の影響を除くと寄与がほぼゼロだった。
= 説明として出していたものが実質デタラメだった。

ユーザー判断 (2026-09-03): 「人気だから9割はやめたい。当たっても安すぎだから」
→ ◎ そのものを説明できるものに置き換える。

## 構造
オッズ・人気を一切使わない23項目を、レース内順位 (1位/2-3位/4-6位/7位以下/データなし)
または5分位に区切り、ロジスティック回帰の係数をそのまま「点数」として足す。
足し算なので「この馬が19点な理由」を1行ずつ言える。

  ◎15番 デビットバローズ 合計 +13点
    騎手の複勝率 (2-3位)   +3.9
    平均スピード指数 (1位)  +3.5
    この距離の実績 (7位以下) -1.1

## 実測 (OOS 2,688レース = 学習境界より後だけで統一比較)
| 印の決め方 | ◎の3着内率 | 印5頭の完全捕捉 |
|---|---|---|
| 点数表 (これ) | 56.9% | 29.5% |
| 現行 (能力ブレンド) | 62.4% | 35.2% |
| 市場 (人気順) | 65.7% | 37.7% |

**的中は上がらない。捕捉を約5.5pt 払って説明可能性を買う取引**。
◎の平均人気は 1.45→1.99、中央オッズ 2.70→3.20 になる。

※ 初版の docstring は「実配信の◎ 56.2% → 点数表 58.1% で引き分け」と書いたが、
  これは点数表(オフライン再現) と 実配信の◎(本番) の**非対称比較**だった
  (本番側にだけ配信経路の劣化 約3pt が乗る)。同条件で比べると符号が逆になる。
  既定は OFF。使うなら MARKS_SCORECARD=1 を明示的に設定する。

## 使ってはいけない項目 (期間で符号が反転するため除外済)
同条件実績・馬番の内外・道悪実績・当コース実績は係数が不安定。
表示に出す項目は STABLE_FOR_DISPLAY に限る。
"""
import os
import pickle

import numpy as np
import pandas as pd

MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "models", "scorecard.pkl")

NODATA = "データなし"
REL_BINS = [(1, 1, "1位"), (2, 3, "2-3位"), (4, 6, "4-6位"), (7, 99, "7位以下")]

# (列名, 表示名, 良い方向, 0はデータ無しか, レース内相対か)
FEATS = [
    ("si_avg",                   "平均スピード指数", "hi", True,  True),
    ("si_latest",                "直近スピード指数", "hi", True,  True),
    ("top3_rate_10r",            "近10走の複勝率",   "hi", True,  True),
    ("avg_finish_5r",            "近5走の平均着順",  "lo", True,  True),
    ("jockey_cond_top3",         "騎手の複勝率",     "hi", True,  True),
    ("trainer_cond_top3",        "厩舎の複勝率",     "hi", True,  True),
    ("course_top3_rate",         "当コース実績",     "hi", True,  True),
    ("same_course_top3_rate",    "同条件実績",       "hi", True,  True),
    ("dist_top3_rate",           "この距離の実績",   "hi", True,  True),
    ("sire_surface_top3_rate",   "父の芝ダ適性",     "hi", True,  True),
    ("damsire_surface_top3_rate", "母父の芝ダ適性",  "hi", True,  True),
    ("avg_last_3f",              "上がり3F平均",     "lo", True,  True),
    ("margin_best",              "自己ベスト着差",   "lo", False, True),
    ("horse_wet_top3_rate",      "道悪実績",         "hi", True,  True),
    ("avg_pos_ratio",            "道中の位置取り",   "lo", False, False),
    ("front_rate",               "逃げ先行率",       "hi", False, False),
    ("rest_days",                "休養日数",         "na", False, False),
    ("distance_diff",            "前走との距離差",   "na", False, False),
    ("jockey_change",            "乗り替わり",       "na", False, False),
    ("impost_diff",              "前走との斤量差",   "na", False, False),
    ("post_position_ratio",      "馬番の内外",       "na", False, False),
    ("graded_exp",               "重賞経験",         "na", False, False),
    ("race_experience",          "キャリア戦数",     "na", False, False),
]
NAMES = {c: n for c, n, _, _, _ in FEATS}

# 期間で係数の符号が反転する = 説明として出す資格がない項目は表示から除く
# (検証で「同条件実績・馬番の内外・道悪実績・当コース実績」が不安定と判明)
UNSTABLE_FOR_DISPLAY = {
    "same_course_top3_rate", "post_position_ratio",
    "horse_wet_top3_rate", "course_top3_rate",
}


def _rel_rank(df, col, direction, zero_na):
    """レース内順位 (1=最良)。データ無しは NaN。"""
    v = pd.to_numeric(df[col], errors="coerce").fillna(0.0).astype(float)
    mask = (v != 0) if zero_na else pd.Series(True, index=df.index)
    tmp = v.where(mask, np.nan)
    return tmp.groupby(df["race_id"]).rank(ascending=(direction == "lo"), method="min")


def label(df, spec):
    """各行を「項目=区分」のラベルに変換する。spec は学習時に決めた区切り。"""
    out = {}
    for col, _, direction, zero_na, is_rel in FEATS:
        if is_rel:
            r = _rel_rank(df, col, direction, zero_na)
            lab = pd.Series(NODATA, index=df.index, dtype=object)
            for lo, hi, nm in REL_BINS:
                lab[(r >= lo) & (r <= hi)] = nm
        else:
            edges = spec[col]
            v = pd.to_numeric(df[col], errors="coerce").fillna(0.0).astype(float)
            lab = pd.Series(
                pd.cut(v, edges, labels=[f"{i+1}/5" for i in range(len(edges) - 1)]).astype(object),
                index=df.index)
            if zero_na:
                lab[v == 0] = NODATA
            lab = lab.fillna(NODATA)
        out[col] = lab
    return pd.DataFrame(out, index=df.index)


def load_model(path=None):
    p = path or MODEL_PATH
    if not os.path.exists(p):
        return None
    with open(p, "rb") as f:
        return pickle.load(f)


def score_with_reasons(df, model, top_n=4):
    """点数と、その内訳を返す。

    戻り値: (点数の配列, 内訳のリスト)
    内訳は [{'label':'騎手の複勝率', 'bin':'2-3位', 'points':3.9}, ...] を
    絶対値の大きい順に top_n 件。**表示不可の項目は除外**するが、
    点数そのものは全項目の合計 (内訳の和 ≠ 合計 になるのは意図通り)。
    """
    pts, spec = model["pts"], model["spec"]
    lab = label(df, spec)
    total = np.zeros(len(df))
    per = [[] for _ in range(len(df))]
    for col, _, _, _, _ in FEATS:
        keys = (col + "=" + lab[col].astype(str)).values
        vals = np.array([pts.get(k, 0.0) for k in keys])
        total += vals
        if col in UNSTABLE_FOR_DISPLAY:
            continue
        for i, (k, v) in enumerate(zip(keys, vals)):
            if abs(v) < 0.02:      # 0点前後は理由にならないので出さない
                continue
            per[i].append({"label": NAMES[col], "bin": k.split("=", 1)[1],
                           "points": round(float(v) * 10, 1)})
    reasons = [sorted(p, key=lambda x: -abs(x["points"]))[:top_n] for p in per]
    return total * 10, reasons     # 点数は ×10 して読みやすく
