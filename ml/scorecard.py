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

# ── 連続項目 (v2, 2026-10) ──────────────────────────────────────────────
# 区分点 (上の FEATS) の「近5走の平均着順」「自己ベスト着差」を外し (model["drop"])、
# 代わりにレース内 z スコア × 係数 (model["lin"]) で足す項目。
# 値は ml/features.recent_form_features (学習側 fast_train と同じ関数) が作る。
#   form_cls5  近5走の 着順/18 − 0.06×(その走の格 − 今回の格) の平均 (小さいほど良い)
#   tb_min5    近5走の 勝ち馬とのタイム差 (秒, 0〜3) の最小       (小さいほど良い)
# 学習外 (A=2025-05-11〜12-31 で当てはめ → B=2026-01-01〜10-18 の 2,700R で評価、現行比):
#   印5頭の平均捕捉 +0.024頭 [+0.008,+0.039] / 完全捕捉 +1.4pt [+0.4,+2.4] / ◎3着内 +0.8pt [-0.2,+1.8]
# 内訳: 格の補正を外した版 (近5走の着順の連続値化・タイム差・初出走の減点だけ) で既に
#   +0.018 [+0.003,+0.033]。格の補正の上乗せは +0.006 [-0.005,+0.016] で区間が0を跨ぐ
#   (◎3着内は +0.8pt [-0.1,+1.6])。= 主効果は格の補正以外。格の補正は方向は正だが
#   単独では有意と言えない (2026-10-19 再計測。作業前の想定「格の補正が本体」とは異なる)。
# ⚠️ ◎ が人気馬に寄る (同じ評価 B、確定人気で数えた記述統計):
#   ◎=1番人気 52.2%→55.3% (+3.1pt [+1.9,+4.3]) / ◎の平均人気 2.02→1.87。#147 で点数表を入れた時に
#   1.45→1.99 へ下げた ◎ の平均人気を約3割戻す (新馬・未勝利でも、それ以外でも同じ方向)。
#   ユーザー要望「人気だから9割はやめたい」(#147)「人気を弱くして」(#108) と逆向きなので、出荷は製品判断。
#   近走の着順を使う版ほど人気に寄る。人気への寄りが最も小さい別構成は「タイム差だけ」
#   (scripts/train_scorecard.py --mode lin --lin tb_min5 --drop margin_best = 近5走の平均着順の区分点は残す):
#   捕捉 +0.018 [+0.005,+0.032] / 完全捕捉 +1.2pt [+0.3,+2.1] / ◎3着内 -0.3pt [-1.1,+0.5] /
#   ◎=1番人気 +1.1pt [+0.3,+1.9] (うち新馬・未勝利以外は +0.3pt [-0.9,+1.3] で区間が0を跨ぐ)。
# ⚠️ 上の数値は A だけで当てはめた版の学習外評価。出荷した models/scorecard.pkl は
#   2025-05-11〜2026-10-18 の全期間で当てはめ直したもの (lin_fit_until) で、出荷版そのものには
#   学習外の評価期間が無い。印を採点するバックテストは scripts/oos_window.get_oos_start() の既定
#   (= 2026-10-19 以降) だけを使うこと。
# (列名, 表示名, 良い方向)
LIN_FEATS = [
    # 表示名はダッシュボードの内訳欄 (約6文字、480px 以下は約5文字) に収まる長さにする。
    # 長い名前は後半が「…」で切れて順位が読めなかった (#167 最終レビュー、実測)。
    ("form_cls5", "近走の着順", "lo"),      # 格 (クラス) を考慮した近5走の着順
    ("tb_min5",   "勝ち馬との差", "lo"),    # 近5走の勝ち馬とのタイム差の最小
]
LIN_NAMES = {c: n for c, n, _ in LIN_FEATS}
LIN_DIR = {c: d for c, _, d in LIN_FEATS}
# 過去の確定走が DB に1つも無い馬への専用の減点 (model["debut_penalty"])。
# DB は JRA の走だけなので、地方・海外からの転入初戦もここに入る → 「デビュー戦」ではなく
# 「JRA初出走」と表示する (転入馬にデビュー戦と書くと事実と違う)。
DEBUT_COL = "has_past_runs"
DEBUT_LABEL = "JRA初出走"
DEBUT_BIN = "データなし"

# 期間で係数の符号が反転する = 説明として出す資格がない項目は表示から除く
# (検証で「同条件実績・馬番の内外・道悪実績・当コース実績」が不安定と判明)
UNSTABLE_FOR_DISPLAY = {
    "same_course_top3_rate", "post_position_ratio",
    "horse_wet_top3_rate", "course_top3_rate",
}


# #150: 絶対値項目のビン名を人間語にする。
# 旧実装は 5分位を "1/5".."5/5" とだけ出しており、読者向けの理由として
# 「休養日数 3/5 +1.2点」のような内部表記がそのまま画面に出ていた (#125 の再発)。
# ビン数は学習時の分位で決まるので、**ビン数が一致するときだけ**日本語名を使い、
# 一致しなければ従来表記に落とす (再学習で分位が変わっても壊れない)。
DISPLAY_BINS = {
    "rest_days":       ["中2週以内", "中3〜4週", "1〜2ヶ月", "2〜4ヶ月", "4ヶ月以上(休み明け)"],
    "distance_diff":   ["距離短縮", "ほぼ同距離", "距離延長", "大幅な距離延長"],
    "jockey_change":   ["継続騎乗", "乗り替わり", "乗り替わり"],
    "impost_diff":     ["斤量減・据え置き", "斤量+1kg以内", "斤量+1kg超"],
    "graded_exp":      ["重賞経験なし", "重賞経験あり"],
    "race_experience": ["キャリア3戦以内", "キャリア4〜9戦", "キャリア10〜18戦", "キャリア19〜30戦", "キャリア30戦超"],
    "front_rate":      ["逃げ先行なし", "先行率2割以下", "先行率2割超"],
    "avg_pos_ratio":   ["前めで運ぶ", "やや前", "中団", "後方から"],
    "post_position_ratio": ["最内寄り", "内", "中", "外", "大外寄り"],
}


def display_bin(col, raw_bin, n_bins=None):
    """内部ビン名 ("3/5") を読者向けの日本語に翻訳する。

    **内部キーは変えない** — model['pts'] は学習時に作った "rest_days=3/5" で
    引かれるので、label() 側を日本語にすると点数の参照が全滅して
    絶対値項目の寄与が静かにゼロになる。翻訳は表示の瞬間だけ行う。
    """
    names = DISPLAY_BINS.get(col)
    if not names or not isinstance(raw_bin, str) or "/" not in raw_bin:
        return raw_bin
    try:
        idx = int(raw_bin.split("/")[0]) - 1
    except ValueError:
        return raw_bin
    if n_bins is not None and len(names) != n_bins:
        return raw_bin     # 再学習で分位が変わった → 従来表記に落とす (誤訳を防ぐ)
    return names[idx] if 0 <= idx < len(names) else raw_bin


def _rel_rank(df, col, direction, zero_na):
    """レース内順位 (1=最良)。データ無しは NaN。"""
    v = pd.to_numeric(df[col], errors="coerce").fillna(0.0).astype(float)
    mask = (v != 0) if zero_na else pd.Series(True, index=df.index)
    tmp = v.where(mask, np.nan)
    return tmp.groupby(df["race_id"]).rank(ascending=(direction == "lo"), method="min")


def label(df, spec, skip=()):
    """各行を「項目=区分」のラベルに変換する。spec は学習時に決めた区切り。
    skip: 使わない項目 (model["drop"])。その列が df に無くても落ちない。"""
    out = {}
    for col, _, direction, zero_na, is_rel in FEATS:
        if col in skip:
            continue
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


def required_columns(model):
    """このモデルで点数を出すのに df に必要な列 (race_id は別途)。"""
    drop = set((model or {}).get("drop") or [])
    need = [c for c, *_ in FEATS if c not in drop]
    need += list(((model or {}).get("lin") or {}).keys())
    if (model or {}).get("debut_penalty"):
        need.append(DEBUT_COL)
    return need


def race_z(values, race_ids, direction="hi"):
    """レース内 z スコア (良い方向が正)。データ無し (NaN) と、レース内で
    ばらつきが無い・1頭しかデータが無い場合は 0 (= レース平均扱い)。
    標準偏差は不偏 (ddof=1) — 当てはめ (scripts/train_scorecard.py) と同じ。"""
    v = pd.to_numeric(pd.Series(values), errors="coerce").astype(float)
    v.index = range(len(v))
    s = -v if direction == "lo" else v
    g = s.groupby(np.asarray(race_ids))
    z = (s - g.transform("mean")) / g.transform("std").replace(0, np.nan)
    return z.fillna(0.0).to_numpy(float)


def score_with_reasons(df, model, top_n=4):
    """点数と、その内訳を返す。

    戻り値: (点数の配列, 内訳のリスト)
    内訳は [{'label':'騎手の複勝率', 'bin':'2-3位', 'points':3.9}, ...] を
    絶対値の大きい順に top_n 件。**表示不可の項目は除外**するが、
    点数そのものは全項目の合計 (内訳の和 ≠ 合計 になるのは意図通り)。

    v2 のモデル (model に "lin" がある) では:
      - model["drop"] の区分項目は点を足さない (内訳にも出さない)
      - model["lin"] の連続項目は「レース内 z (データ無し=0) × 係数」を足す
      - model["debut_penalty"] を過去走ゼロ (has_past_runs == 0) の馬に足す
    係数は区分点と同じ単位 (log-odds) に揃えてあるので、点数のスケール (×10) は従来どおり。
    "lin" が無い古い pkl は従来と完全に同じ計算になる。
    """
    pts, spec = model["pts"], model["spec"]
    drop = set(model.get("drop") or [])
    lab = label(df, spec, skip=drop)
    total = np.zeros(len(df))
    per = [[] for _ in range(len(df))]
    for col, _, _, _, _ in FEATS:
        if col in drop:
            continue
        keys = (col + "=" + lab[col].astype(str)).values
        vals = np.array([pts.get(k, 0.0) for k in keys])
        total += vals
        if col in UNSTABLE_FOR_DISPLAY:
            continue
        for i, (k, v) in enumerate(zip(keys, vals)):
            if abs(v) < 0.02:      # 0点前後は理由にならないので出さない
                continue
            _raw = k.split("=", 1)[1]
            _nb = (len(spec[col]) - 1) if col in spec else None
            per[i].append({"label": NAMES[col], "bin": display_bin(col, _raw, _nb),
                           "points": round(float(v) * 10, 1)})

    lin = model.get("lin") or {}
    if lin:
        rid = df["race_id"].to_numpy()
        for col, coef in lin.items():
            if col not in LIN_DIR:
                raise KeyError(f"scorecard: 未知の連続項目 {col} (ml/scorecard.LIN_FEATS に無い)")
            direction = LIN_DIR[col]
            raw = pd.to_numeric(df[col], errors="coerce").astype(float)
            raw.index = range(len(raw))
            contrib = float(coef) * race_z(raw.to_numpy(), rid, direction)
            total += contrib
            rank = raw.groupby(rid).rank(ascending=(direction == "lo"), method="min").to_numpy()
            for i, v in enumerate(contrib):
                if abs(v) < 0.02 or not np.isfinite(rank[i]):
                    continue
                per[i].append({"label": LIN_NAMES[col], "bin": f"{int(rank[i])}位",
                               "points": round(float(v) * 10, 1)})

    dp = float(model.get("debut_penalty") or 0.0)
    if dp:
        has = pd.to_numeric(df[DEBUT_COL], errors="coerce").fillna(1).to_numpy(float)
        debut = has == 0
        # 全頭が初出走のレース (新馬戦など) では減点が全頭に同じ値で乗るだけで順位は
        # 変わらない。そこで点を引くと全頭に「JRA初出走 −14点」という差のつかない
        # 理由が並び、合計点も全頭マイナスになって読み違えを招くので、足さない。
        # (当てはめのソフトマックスもレース内で一定の項は無視するので、係数は同じ)
        any_past = pd.Series(~debut).groupby(df["race_id"].to_numpy()).transform("any").to_numpy()
        debut = debut & any_past
        total += np.where(debut, dp, 0.0)
        if abs(dp) >= 0.02:
            for i in np.flatnonzero(debut):
                per[i].append({"label": DEBUT_LABEL, "bin": DEBUT_BIN,
                               "points": round(dp * 10, 1)})

    reasons = [sorted(p, key=lambda x: -abs(x["points"]))[:top_n] for p in per]
    return total * 10, reasons     # 点数は ×10 して読みやすく
