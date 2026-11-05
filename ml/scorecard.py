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

# ── 生の値 × 係数で足す項目 (v3, 2026-10 #168) ─────────────────────────
# model["flags"] = {列名: 係数}。点 = 係数 × 値 (NaN は 0)。lin と違ってレース内 z を取らない
# (0/1 の「初ダート」はそのまま、割合はそのまま足す)。値は ml/speed_variant.surface_switch_features
# (学習側 fast_train と推論側 ml/features の同じ関数) が作る。
#   sf_first_switch  過去の確定平地走があり、今回の芝ダの走が0 (=初ダート/初芝) なら 1
#   sf_other_share5  近5走 (確定平地走) のうち今回と違う芝ダの走の割合 (0〜1)
# (列名, 表示名, 区分の表示)
FLAG_FEATS = [
    ("sf_first_switch", "芝ダ初替り", "first"),
    ("sf_other_share5", "芝ダ替わり", "share"),
]
FLAG_NAMES = {c: n for c, n, _ in FLAG_FEATS}
FLAG_KIND = {c: k for c, _, k in FLAG_FEATS}

# 期間で係数の符号が反転する = 説明として出す資格がない項目は表示から除く
# (検証で「同条件実績・馬番の内外・道悪実績・当コース実績」が不安定と判明)
UNSTABLE_FOR_DISPLAY = {
    "same_course_top3_rate", "post_position_ratio",
    "horse_wet_top3_rate", "course_top3_rate",
}


# #150: 絶対値項目のビン名を人間語にする。
# 旧実装は 5分位を "1/5".."5/5" とだけ出しており、読者向けの理由として
# 「休養日数 3/5 +1.2点」のような内部表記がそのまま画面に出ていた (#125 の再発)。
#
# #169: 表示名は **区分境界 (model["spec"]) から作る**。#150 の固定の名前表は
# v3 の当て直しで「道中の位置取り」が4→5区分になった時に段数が合わず、内部表記の
# 「5/5」が JSON と画面に戻っていた。しかも段数が合っていても中身がずれていた:
# キャリア戦数は #166 で「近10走の数 /10」に変わったのに名前は「/30」の時代のまま
# (1戦以下を「キャリア3戦以内」、4〜6戦を「キャリア10〜18戦」と表示していた)。
# 境界の値から名前を作れば、当て直しで分位が変わっても名前が実際の区切りを指す。
# 名前を作れない区分は「内部表記で出す」のではなく **内訳に出さない** (点は合計に入る)。
# 段数・重複・内部表記の検査は check_display_bins (train_scorecard / feature_parity / predict)。
_INF = float("inf")
RAW_BIN_RE = r"^\d+/\d+$"          # 内部キーの区分名 ("5/5")。表示に出てはいけない


def _num(x):
    """1.0 → "1", 0.5 → "0.5" (表示用)。"""
    r = round(float(x), 1)
    return str(int(r)) if r == int(r) else str(r)


def _intervals(edges):
    return list(zip(edges[:-1], edges[1:]))


def _names_rest_days(edges):
    # 値 = 前走からの日数 / 365 (上限365日)。表示名の「休養日数」に合わせて日数で示す。
    out = []
    for lo, hi in _intervals(edges):
        dlo = None if lo == -_INF else int(round(lo * 365))
        dhi = None if hi == _INF else int(round(hi * 365))
        if dlo is None:
            out.append(f"{dhi}日以内")
        elif dhi is None:
            out.append(f"{int(round(dlo / 30.4))}ヶ月超(休み明け)" if dlo >= 90 else f"{dlo + 1}日以上")
        else:
            out.append(f"{dlo + 1}〜{dhi}日")
    return out


def _names_distance_diff(edges):
    # 値 = (今回の距離 − 前走の距離) / 400
    out = []
    for lo, hi in _intervals(edges):
        mlo = None if lo == -_INF else int(round(lo * 400))
        mhi = None if hi == _INF else int(round(hi * 400))
        if mlo is None:
            out.append(f"短縮{-mhi}m以上" if mhi < 0 else f"延長{mhi}m以下")
        elif mhi is None:
            out.append(f"延長{mlo}m超" if mlo >= 0 else f"短縮{-mlo}m未満〜延長")
        elif mlo >= -100 and mhi <= 100 and mlo < 0 <= mhi:
            out.append("ほぼ同距離")
        elif mlo >= 0:
            out.append(f"延長{mhi}m以内" if mlo == 0 else f"延長{mlo}超〜{mhi}m")
        else:
            out.append(f"短縮{-mhi}〜{-mlo}m" if mhi < 0 else f"{mlo:+d}〜{mhi:+d}m")
    return out


def _names_jockey_change(edges):
    # 値 = 0 (前走と同じ騎手) / 1 (乗り替わり)。どちらも含まない区分は到達しない (None)
    out = []
    for lo, hi in _intervals(edges):
        if lo < 0 <= hi:
            out.append("継続騎乗")
        elif lo < 1 <= hi:
            out.append("乗り替わり")
        else:
            out.append(None)
    return out


def _names_impost_diff(edges):
    # 値 = 今回の斤量 − 前走の斤量 (kg)
    out = []
    for lo, hi in _intervals(edges):
        if lo == -_INF:
            out.append("斤量減・据え置き" if hi == 0 else
                       (f"斤量{_num(-hi)}kg以上減" if hi < 0 else f"斤量+{_num(hi)}kg以下"))
        elif hi == _INF:
            out.append(f"斤量+{_num(lo)}kg超" if lo >= 0 else f"斤量{_num(-lo)}kg減未満〜増")
        elif lo >= 0:
            out.append(f"斤量+{_num(hi)}kg以内" if lo == 0 else f"斤量+{_num(lo)}超〜{_num(hi)}kg")
        else:
            out.append(f"斤量{_num(-hi)}〜{_num(-lo)}kg減" if hi <= 0 else f"斤量{_num(lo)}〜+{_num(hi)}kg")
    return out


def _count_names(edges, cap, unit, zero):
    """値 = min(回数, cap) / cap の項目 (重賞経験・キャリア戦数)。回数で名前を付ける。"""
    out = []
    for lo, hi in _intervals(edges):
        clo = None if lo == -_INF else int(round(lo * cap))
        chi = None if hi == _INF else int(round(hi * cap))
        if clo is not None and clo >= cap:
            out.append(None)                       # 上限 (cap) を超える値は無い = 到達しない
        elif clo is None:
            out.append(zero if chi <= 0 else f"{chi}{unit}以下")
        elif chi is None or chi >= cap:
            out.append(f"{clo + 1}{unit}以上")
        elif clo + 1 == chi:
            out.append(f"{chi}{unit}")
        else:
            out.append(f"{clo + 1}〜{chi}{unit}")
    return out


def _names_graded_exp(edges):
    names = _count_names(edges, 10, "戦", "なし")
    # 2区分 (なし / 1戦以上) は「重賞経験なし / あり」の方が読みやすい
    return ["重賞経験" + ("なし" if n == "なし" else ("あり" if n == "1戦以上" else n)) if n else None
            for n in names]


def _names_race_experience(edges):
    # #166 以降の値は「DB の確定走 (近10走まで) の数 / 10」。表示名「キャリア戦数」に戦数で続ける
    return _count_names(edges, 10, "戦", "なし")


def _names_front_rate(edges):
    # 値 = 近5走のうち1コーナーを前35%以内で回った走の割合
    out = []
    for lo, hi in _intervals(edges):
        wlo = None if lo == -_INF else int(round(lo * 10))
        whi = None if hi == _INF else int(round(hi * 10))
        if wlo is None:
            out.append("なし" if whi <= 0 else f"{whi}割以下")
        elif whi is None:
            out.append(f"{wlo}割超")
        elif wlo <= 0:
            out.append(f"{whi}割以下")
        else:
            out.append(f"{wlo}〜{whi}割")
    return out


def _position_names(edges, words):
    """0〜1 の位置 (0=前・内) の項目。区分の代表位置 (区間の中点) に言葉を当て、
    言葉が重なったら境界の % で名前を付ける (どちらも境界から作る)。"""
    iv = _intervals(edges)
    out = []
    for lo, hi in iv:
        rep = (max(lo, 0.0) + min(hi, 1.0)) / 2
        out.append(next(w for th, w in words if rep <= th))
    if len(set(out)) == len(out):
        return out
    out = []
    for lo, hi in iv:
        plo = None if lo == -_INF else int(round(lo * 100))
        phi = None if hi == _INF else int(round(hi * 100))
        out.append(f"前{phi}%以内" if plo is None else (f"前{plo}%より後ろ" if phi is None
                                                        else f"前{plo}〜{phi}%"))
    return out


def _names_avg_pos_ratio(edges):
    # 値 = 近5走の1コーナー通過順 / 頭数 の平均 (0=先頭)
    return _position_names(edges, ((0.25, "先行"), (0.42, "好位"), (0.55, "中団"),
                                   (0.70, "中団後ろ"), (_INF, "後方")))


def _names_post_position_ratio(edges):
    # 値 = 馬番 / 頭数
    return _position_names(edges, ((0.2, "最内寄り"), (0.4, "内"), (0.6, "中"),
                                   (0.8, "外"), (_INF, "大外寄り")))


_BIN_NAMERS = {
    "rest_days": _names_rest_days,
    "distance_diff": _names_distance_diff,
    "jockey_change": _names_jockey_change,
    "impost_diff": _names_impost_diff,
    "graded_exp": _names_graded_exp,
    "race_experience": _names_race_experience,
    "front_rate": _names_front_rate,
    "avg_pos_ratio": _names_avg_pos_ratio,
    "post_position_ratio": _names_post_position_ratio,
}


def bin_display_names(col, edges):
    """区分境界 edges (model["spec"][col]) から各区分の表示名を作る (#169)。
    戻り値は区分と同じ数のリスト (到達しない区分は None)。作れない項目は None。"""
    f = _BIN_NAMERS.get(col)
    if f is None or edges is None or len(edges) < 2:
        return None
    try:
        return f([float(e) for e in edges])
    except Exception:
        return None


def check_display_bins(model):
    """区分の表示名が区分境界と合っているか (#169)。問題の文のリスト (空なら OK)。

    - 表示名を作れない項目がある / 名前の数が区分数と違う
    - 点の付いた (= 内訳に出うる) 区分に名前が無い
    - 名前が重なる (別の区分が同じ名前に見える) / 内部表記 ("5/5") になっている
    """
    import re
    probs = []
    spec = (model or {}).get("spec") or {}
    pts = (model or {}).get("pts") or {}
    drop = set((model or {}).get("drop") or [])
    for col, name, _, _, is_rel in FEATS:
        if is_rel or col in drop or col not in spec:
            continue
        edges = spec[col]
        nb = len(edges) - 1
        names = bin_display_names(col, edges)
        if names is None:
            probs.append(f"{name} ({col}): 区分 {nb} 段の表示名を作れない (ml/scorecard._BIN_NAMERS に無い)")
            continue
        if len(names) != nb:
            probs.append(f"{name} ({col}): 区分 {nb} 段に対し表示名 {len(names)} 個")
            continue
        for i, nm in enumerate(names):
            k = f"{col}={i + 1}/5"
            if nm is None and abs(pts.get(k, 0.0)) >= 0.02:
                probs.append(f"{name} ({col}): 点の付いた区分 {i + 1}/{nb} に表示名が無い")
        real = [n for n in names if n is not None]
        if len(set(real)) != len(real):
            probs.append(f"{name} ({col}): 表示名が重なる {real}")
        bad = [n for n in real if re.match(RAW_BIN_RE, n)]
        if bad:
            probs.append(f"{name} ({col}): 表示名が内部表記 {bad}")
    return probs


_DISPLAY_WARNED = set()


def display_bin(col, raw_bin, names=None):
    """内部ビン名 ("3/5") を読者向けの日本語に翻訳する。

    **内部キーは変えない** — model['pts'] は学習時に作った "rest_days=3/5" で
    引かれるので、label() 側を日本語にすると点数の参照が全滅して
    絶対値項目の寄与が静かにゼロになる。翻訳は表示の瞬間だけ行う。

    #169: names は bin_display_names(col, 区分境界) の結果。名前が無い区分は
    内部表記を返さず None (= 内訳に出さない) にし、1回だけ警告する。
    順位の区分 ("1位" 等) や「データなし」はそのまま返す。
    """
    if not isinstance(raw_bin, str) or "/" not in raw_bin:
        return raw_bin
    try:
        idx = int(raw_bin.split("/")[0]) - 1
    except ValueError:
        idx = -1
    nm = names[idx] if names and 0 <= idx < len(names) else None
    if nm is None and (col, raw_bin) not in _DISPLAY_WARNED:
        _DISPLAY_WARNED.add((col, raw_bin))
        print(f"  ⚠️ 点数表: {NAMES.get(col, col)} の区分 {raw_bin} に表示名が無い "
              f"→ 内訳に出さない (#169、点は合計に入る)")
    return nm


def _src(col, col_map=None):
    """区分項目 col の値を読む列名。model["col_map"] で差し替える (例 si_avg → si_avg_v)。
    点数のキー ("si_avg=1位") と表示名は元の項目名のまま (後方互換)。"""
    return (col_map or {}).get(col, col)


def _rel_rank(df, col, direction, zero_na):
    """レース内順位 (1=最良)。データ無しは NaN。"""
    v = pd.to_numeric(df[col], errors="coerce").fillna(0.0).astype(float)
    mask = (v != 0) if zero_na else pd.Series(True, index=df.index)
    tmp = v.where(mask, np.nan)
    return tmp.groupby(df["race_id"]).rank(ascending=(direction == "lo"), method="min")


def label(df, spec, skip=(), col_map=None):
    """各行を「項目=区分」のラベルに変換する。spec は学習時に決めた区切り。
    skip: 使わない項目 (model["drop"])。その列が df に無くても落ちない。
    col_map: 項目の値を読む列の差し替え (model["col_map"])。ラベルの列名は元の項目名のまま。"""
    out = {}
    for col, _, direction, zero_na, is_rel in FEATS:
        if col in skip:
            continue
        src = _src(col, col_map)
        if is_rel:
            r = _rel_rank(df, src, direction, zero_na)
            lab = pd.Series(NODATA, index=df.index, dtype=object)
            for lo, hi, nm in REL_BINS:
                lab[(r >= lo) & (r <= hi)] = nm
        else:
            edges = spec[col]
            v = pd.to_numeric(df[src], errors="coerce").fillna(0.0).astype(float)
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
    cmap = (model or {}).get("col_map") or {}
    need = [_src(c, cmap) for c, *_ in FEATS if c not in drop]
    need += list(((model or {}).get("lin") or {}).keys())
    need += list(((model or {}).get("flags") or {}).keys())
    if (model or {}).get("debut_penalty"):
        need.append(DEBUT_COL)
    return need


def check_si_variant(model, table_path=None):
    """点数表が V2d (model["si_variant"]) を前提にしているのに、基準表が無い・読めない・違う時の警告文。
    問題なければ None。基準表が違うと si_avg_v の値が当てはめ時と別物になる (#134 型の silent 劣化)。
    predict はこの文が返ると without_col_map で代用モデルに切り替え、verify_feature_parity は致命にする。"""
    meta = (model or {}).get("si_variant")
    if not meta:
        return None
    try:
        from ml import speed_variant as _sv
        t = _sv.load_table(table_path)
    except Exception as e:
        return f"V2d の基準表が読めない ({e})"
    if t is None:
        return "V2d の基準表 models/si_variant_table.json が無い — si_avg_v 等が作られない"
    if t.get("table_hash") != meta.get("table_hash"):
        return (f"V2d の基準表が点数表の当てはめ時と違う (表 {t.get('table_hash')} cutoff {t.get('cutoff')} "
                f"c={t.get('dirt_shift_c')} / 点数表 {meta.get('table_hash')} cutoff {meta.get('cutoff')} "
                f"c={meta.get('dirt_shift_c')})")
    return None


def without_col_map(model, reason):
    """V2d が使えない時の代用モデル: 区分の差し替え (col_map) を外し、指数の区分を現行の
    si_avg / si_latest (LGB と同じ列、学習↔推論の照合済み) で読む。区分点・連続項目・flags は
    そのまま。印を市場寄りの従来ロジック (pred_top3) に落とすより、点数表の印のまま
    「指数だけ近い代用品」にする方が変化が小さい (#168 レビュー)。model["_degraded"] に理由を残す。"""
    m = dict(model or {})
    m["col_map"] = {}
    m["_degraded"] = reason
    return m


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


# ── 内訳の表示 (#169) ─────────────────────────────────────────────────
# 点は一切変えず、内訳に「何と書くか」「出すか」だけを決める。
# 過去走が無い馬 (has_past_runs == 0) では、前走・近走から作る項目の値は「既定値」で
# 実測ではない (休養日数は 0.5 = 約182日が入り「4ヶ月超(休み明け)」と表示されていた、
# 距離差・乗り替わりは 0 で「ほぼ同距離」「継続騎乗」になる)。事実どおり JRA初出走 と書く。
NO_PAST_DEFAULT_COLS = {"rest_days", "distance_diff", "jockey_change", "impost_diff",
                        "avg_pos_ratio", "front_rate"}


def _reason_context(df):
    """内訳の表示に使う補助の値 (点数には使わない)。列が無ければ従来どおりの表示になる。"""
    n = len(df)
    rid = df["race_id"].to_numpy() if "race_id" in df.columns else np.zeros(n)
    if DEBUT_COL in df.columns:
        has_past = pd.to_numeric(df[DEBUT_COL], errors="coerce").fillna(1).to_numpy(float) != 0
    else:
        has_past = np.ones(n, bool)
    # 近10走の確定走の数 (= race_experience × 10、#166 の定義)。top3_rate_10r の分母
    n10 = (np.rint(pd.to_numeric(df["race_experience"], errors="coerce").fillna(0)
                   .to_numpy(float) * 10).astype(int)
           if "race_experience" in df.columns else np.zeros(n, int))
    # この距離の実績: 0/1 = 同距離帯 (±200m、無ければ ±400m) に走がある、2 = 無い
    dscope = (pd.to_numeric(df["dist_scope"], errors="coerce").fillna(2).to_numpy(float)
              if "dist_scope" in df.columns else np.full(n, 2.0))
    return {"rid": rid, "has_past": has_past, "n10": n10, "dscope": dscope}


def _race_common(values, rid):
    """各行について「同じレースの全頭が同じ値 (区分)」か。1頭だけのレースは False。"""
    s = pd.Series(np.asarray(values))
    g = s.groupby(np.asarray(rid))
    return ((g.transform("nunique") == 1) & (g.transform("size") > 1)).to_numpy()


def _reason_bin(col, raw, names, ctx, i):
    """内訳に出す区分の文字。None なら出さない。"""
    if not ctx["has_past"][i] and col in NO_PAST_DEFAULT_COLS:
        return DEBUT_LABEL
    if raw == NODATA:
        # 0 を「データ無し」と同じ点で扱う項目 (zero_na)。点はそのままに、実際に走っていて
        # 3着内が0回の馬は「データなし」でなく実態を書く (今週53頭、うち印5頭に14頭)。
        if col == "top3_rate_10r" and ctx["has_past"][i] and ctx["n10"][i] > 0:
            return f"3着内なし({ctx['n10'][i]}走)"
        if col == "dist_top3_rate" and ctx["dscope"][i] in (0, 1):
            return "3着内なし"
        return raw
    return display_bin(col, raw, names)


# 馬自身の過去走から作る項目 (初出走の馬は値が全頭同じ既定値 / データなしになる)。
# 新馬戦 (全頭が初出走) では全頭同じ区分になり、新しい内訳では _race_common で出ない。
HORSE_HISTORY_COLS = {"si_avg", "si_latest", "top3_rate_10r", "avg_finish_5r",
                      "course_top3_rate", "same_course_top3_rate", "dist_top3_rate",
                      "avg_last_3f", "margin_best", "horse_wet_top3_rate",
                      "graded_exp", "race_experience"} | NO_PAST_DEFAULT_COLS
_SANITIZE_MODEL = []


def sanitize_cached_reasons(horses, race_name="", model=None):
    """#169 より前に作られたキャッシュの sc_reasons を、今の表示規則にそろえる (export 用)。

    #169 手直し: 新しい内訳は predict の時点で規則どおりに作られるが、古いキャッシュを
    再 export すると内部表記の「道中の位置取り 5/5」(10/31・11/01 で17件) と、新馬戦の
    初出走馬への「休養日数 休み明け」(同79件) が JSON にそのまま出ていた。
      - 内部表記の区分 ("5/5") は、キャッシュの点数が **今の点数表の同じ区分の点と一致した
        時だけ** 区分境界から作った表示名に訳す (= 同じ点数表で作られた証拠がある時だけ)。
        合わなければ (別の版の点数表) 訳さず消す
      - 新馬戦 (全頭が初出走) は馬の過去走から作る項目が全頭同じ = 理由にならないので消す
        (新しい内訳の「レース内の全頭が同じ区分は出さない」と同じ結果)
    点数・印は変えない。新しいキャッシュには何もしない (該当する項目が無い)。
    戻り値: (訳した数, 消した数)。"""
    import re
    if model is None:
        if not _SANITIZE_MODEL:
            try:
                _SANITIZE_MODEL.append(load_model())
            except Exception:
                _SANITIZE_MODEL.append(None)
        model = _SANITIZE_MODEL[0]
    spec = (model or {}).get("spec") or {}
    pts = (model or {}).get("pts") or {}
    by_label = {n: c for c, n in NAMES.items()}
    debut_race = "新馬" in str(race_name or "")
    hist_labels = {NAMES[c] for c in HORSE_HISTORY_COLS if c in NAMES}
    n_tr = n_drop = 0
    for h in horses or []:
        rs = h.get("sc_reasons")
        if not rs:
            continue
        out = []
        for r in rs:
            lab, b = r.get("label"), str(r.get("bin"))
            if debut_race and lab in hist_labels:
                n_drop += 1
                continue
            if re.match(RAW_BIN_RE, b):
                col = by_label.get(lab)
                names = bin_display_names(col, spec.get(col)) if col in spec else None
                key = f"{col}={b}"
                try:
                    same = key in pts and abs(round(pts[key] * 10, 1) - float(r.get("points"))) < 0.051
                    idx = int(b.split("/")[0]) - 1
                except (TypeError, ValueError):
                    same, idx = False, -1
                nm = names[idx] if (same and names and 0 <= idx < len(names)) else None
                if nm is None:
                    n_drop += 1
                    continue
                r = dict(r, bin=nm)
                n_tr += 1
            out.append(r)
        h["sc_reasons"] = out
    return n_tr, n_drop


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

    v3 (#168) のモデルでは:
      - model["col_map"] で区分項目の値を読む列を差し替える (例 si_avg → si_avg_v)。
        点数のキー ("si_avg=1位") と表示名は元の項目名のまま
      - model["flags"] の項目は「係数 × 生の値」を足す (芝ダ替わり)
    col_map / flags が無い pkl は従来と完全に同じ計算になる。

    #169 (表示だけ。点数は従来と完全に同じ):
      - 区分の表示名は区分境界から作る (bin_display_names)。名前の無い区分は内訳に出さない
      - レース内の全頭が同じ区分の項目 (新馬戦の「データなし」「休み明け」等) は内訳に出さない
      - 過去走の無い馬の前走・近走由来の項目は「JRA初出走」、走っていて3着内0回は
        「3着内なし(N走)」と書く (「休み明け」「データなし」と書かない)
    """
    pts, spec = model["pts"], model["spec"]
    drop = set(model.get("drop") or [])
    lab = label(df, spec, skip=drop, col_map=model.get("col_map"))
    total = np.zeros(len(df))
    per = [[] for _ in range(len(df))]
    # #169 手直し: 表示だけの処理 (_reason_context / _race_common / _reason_bin) の例外で
    # 点数を止めない。止めると predict._attach_scorecard の except で全頭 0点 = そのレースの
    # 印が従来ロジックに落ち、表示の不具合が印の変化に化ける。失敗したら内訳だけ空にする。
    _disp_err = []
    try:
        _ctx = _reason_context(df)                                     # #169 表示用 (点は変えない)
        _names = {c: bin_display_names(c, spec[c]) for c in spec}
    except Exception as e:
        _disp_err.append(e)
        _ctx, _names = None, {}
    for col, _, _, _, _ in FEATS:
        if col in drop:
            continue
        keys = (col + "=" + lab[col].astype(str)).values
        vals = np.array([pts.get(k, 0.0) for k in keys])
        total += vals
        if col in UNSTABLE_FOR_DISPLAY or _disp_err:
            continue
        try:
            _common = _race_common(keys, _ctx["rid"])
            for i, (k, v) in enumerate(zip(keys, vals)):
                if abs(v) < 0.02:      # 0点前後は理由にならないので出さない
                    continue
                if _common[i]:         # #169: レース内の全頭が同じ区分 = 差のつかない項目は理由にならない
                    continue
                _bin = _reason_bin(col, k.split("=", 1)[1], _names.get(col), _ctx, i)
                if _bin is None:       # #169: 表示名の無い区分は内部表記で出さない
                    continue
                per[i].append({"label": NAMES[col], "bin": _bin,
                               "points": round(float(v) * 10, 1)})
        except Exception as e:
            _disp_err.append(e)

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

    flags = model.get("flags") or {}
    if flags:
        turf = (pd.to_numeric(df["surface_turf"], errors="coerce").to_numpy(float)
                if "surface_turf" in df.columns else np.full(len(df), np.nan))
        for col, coef in flags.items():
            if col not in FLAG_KIND:
                raise KeyError(f"scorecard: 未知の項目 {col} (ml/scorecard.FLAG_FEATS に無い)")
            x = pd.to_numeric(df[col], errors="coerce").fillna(0.0).to_numpy(float)
            contrib = float(coef) * x
            total += contrib
            if _disp_err:
                continue                                                # 内訳は最後に空にする
            try:
                _common = _race_common(x, _ctx["rid"])                 # #169 区分項目と同じ扱い
            except Exception as e:
                _disp_err.append(e)
                continue
            for i, v in enumerate(contrib):
                if abs(v) < 0.02 or x[i] == 0 or _common[i]:
                    continue
                if FLAG_KIND[col] == "first":
                    lab_i = ("初ダート" if turf[i] == 0 else "初芝") if np.isfinite(turf[i]) \
                        else FLAG_NAMES[col]
                    bin_i = "替わり初戦"
                else:
                    lab_i = FLAG_NAMES[col]
                    bin_i = f"他馬場{int(round(x[i] * 100))}%"
                per[i].append({"label": lab_i, "bin": bin_i, "points": round(float(v) * 10, 1)})

    if _disp_err:
        # #169 手直し: 内訳の表示処理だけが失敗 → 点数 (= 印) はそのまま、内訳は出さない
        print(f"  ⚠️ 点数表: 内訳の表示処理に失敗 ({_disp_err[0]!r}) → 内訳は出さず点数だけ使う (#169)")
        return total * 10, [[] for _ in per]
    reasons = [sorted(p, key=lambda x: -abs(x["points"]))[:top_n] for p in per]
    return total * 10, reasons     # 点数は ×10 して読みやすく
