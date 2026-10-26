#!/usr/bin/env python3
"""OOS 評価窓の単一情報源 (#141)。

CLAUDE.md #109 は「モデル出力を使う backtest は fast_train の 80/20 学習境界
(当時 ~2025-02) 以降だけを評価窓にする」と定めたが、この日付は**ハードコードされ**、
モデルが再学習されるたびに実際の境界が動くのに追従していなかった。

さらに #141 の監査で、分割そのものが時系列になっていなかったことが判明した
(sort_values("race_id") は race_id が「年+場コード+開催回+日+R」のため
 日付順でなく場コード順にソートする)。実測で学習側に 2025-03 以降が
1,114レース混入し、学習側の最大日付は 2025-11-24 だった。
つまり #109 導入以降のすべてのバックテストが静かに in-sample を混ぜていた。

#157: 本番の ◎○▲△△ は点数表 (models/scorecard.pkl) の上位5頭で決まる。点数表は
rank 系 (model_rank/top3/win) とは別の日付まで学習・当てはめされており、
train_boundary.json は rank 系しか記録していなかった。点数表を新しい日付で
当てはめ直した瞬間に、ここが黙って楽観的な窓を返すことになる。
→ 窓の開始は「rank 系の学習境界」と「点数表の学習・当てはめ境界
   (train_until / v3 の pts_fit_until と lin_fit_until / V2d 基準表のカットオフ の最も遅い日、#168)」
   の **遅い方** の翌日にする。

ただし点数表を使わない (rank 系のモデルだけを採点する) バックテストまで点数表の境界に
巻き込むと、点数表を全期間で当てはめ直した瞬間に学習外の期間がゼロになる。
→ include_scorecard=False で rank 系の境界だけの窓を取れるようにする。
   **印 (◎○▲△△) や点数表の点数を採点するなら必ず既定の include_scorecard=True**。
窓に確定レースが1件も無い (= 学習外の期間が無い) 時は警告を出す
(空の窓を黙って返すと、バックテストが0件のまま「問題なし」に見える)。

以降、backtest は必ずこのモジュール経由で窓を取得すること。
"""
import json
import os
import pickle
import sys
import warnings
from datetime import datetime, timedelta

_MODELS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
_BOUNDARY = os.path.join(_MODELS, "train_boundary.json")
_SCORECARD = os.path.join(_MODELS, "scorecard.pkl")


def _next_day(d):
    return (datetime.strptime(d[:10], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def _rank_meta():
    if not os.path.exists(_BOUNDARY):
        raise RuntimeError(
            "models/train_boundary.json がありません。fast_train を1度実行するか、"
            "バックテストの評価窓を手で確定させてください。"
            "固定日のハードコードは #141 で禁止されました。")
    with open(_BOUNDARY, encoding="utf-8") as f:
        return json.load(f)


# 点数表 (pkl) が見たデータの最終日を表すキー。v3 (#168 の二段 F2) で pts_fit_until が増えた。
# 区分点の窓 (pts_fit_until = train_until) と連続項目・芝ダ替わりの窓 (lin_fit_until) の遅い方。
_SC_DATE_KEYS = ("train_until", "pts_fit_until", "lin_fit_until")


def _scorecard_dates(m):
    """点数表が見た最後の日付の候補 {キー: 日付}。V2d の基準表のカットオフ (si_variant.cutoff、
    μ̂・κ̂・c を決めたデータの最終日) も含める。"""
    out = {k: str(m.get(k))[:10] for k in _SC_DATE_KEYS if m.get(k)}
    sv = m.get("si_variant") or {}
    if sv.get("cutoff"):
        out["si_variant.cutoff"] = str(sv["cutoff"])[:10]
    return out


def scorecard_boundary(path=None):
    """点数表が見た最後の日付 (YYYY-MM-DD)。train_until / pts_fit_until (区分点の学習) と
    lin_fit_until (連続項目・芝ダ替わりの当てはめ) と V2d 基準表のカットオフの最も遅い日。
    点数表が無ければ None。

    pkl が読めない時は例外にする — 黙って None を返すと rank 系の境界だけで
    楽観的な窓を返してしまう (#157 の失敗そのもの)。"""
    p = path or _SCORECARD
    if not os.path.exists(p):
        return None
    with open(p, "rb") as f:
        m = pickle.load(f)
    ds = list(_scorecard_dates(m).values())
    return max(ds) if ds else None


def window_race_count(start, db_path=None):
    """start 以降の確定レース数 (着順>0 の行があるレース)。DB が読めなければ None。
    db_path を省略すると database.DB_PATH (= 呼び出し側が差し替えた DB) を読む。"""
    try:
        import sqlite3
        if db_path is None:
            sys.path.insert(0, os.path.dirname(_MODELS))
            import database
            db_path = database.DB_PATH
        if not os.path.exists(db_path):
            return None
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            n = c.execute(
                """SELECT COUNT(DISTINCT r.race_id) FROM results r
                   JOIN races ra ON ra.race_id = r.race_id
                   WHERE r.finish_position > 0 AND ra.race_date >= ?""", (start,)).fetchone()[0]
        finally:
            c.close()
        return int(n)
    except Exception:
        return None


def get_oos_start(strict=True, scorecard_path=None, include_scorecard=True,
                  warn_empty=True, db_path=None):
    """OOS 評価窓の開始日 (YYYY-MM-DD)。この日以降のレースだけが真の out-of-sample。

    strict=True (既定): rank 系の学習側の最大日付の翌日 = 混入ゼロを保証する保守的な窓。
    strict=False: rank 系は val 側の最小日付 (分割が正しければ同じ、壊れていれば楽観的)。
    include_scorecard=True (既定): 点数表の学習・当てはめ境界も効かせ、遅い方の翌日にする。
        **印 (◎○▲△△ = 点数表の上位5頭) や点数表の点数を採点するならこれ**。
        点数表の境界は strict に関係なく効く。
    include_scorecard=False: rank 系 (model_rank/top3/win) の境界だけ。点数表を一切使わない
        バックテスト用 (点数表を全期間で当てはめ直しても窓が消えない)。
    warn_empty: 窓に確定レースが0件なら警告 (UserWarning) を出す。DB が読めない時は黙る。
    """
    meta = _rank_meta()
    start = _next_day(meta["train_max_date"]) if strict else meta["val_min_date"]
    if include_scorecard:
        sc = scorecard_boundary(scorecard_path)
        if sc:
            start = max(start, _next_day(sc))
    if warn_empty:
        n = window_race_count(start, db_path)
        if n == 0:
            which = "rank系と点数表の遅い方" if include_scorecard else "rank系"
            warnings.warn(
                f"OOS 評価窓 ({start} 以降, 境界={which}) に確定レースが0件 — 学習外の期間が無い。"
                f"このままバックテストすると0件の採点になる。"
                + ("点数表を使わないなら include_scorecard=False を検討。" if include_scorecard else ""),
                UserWarning, stacklevel=2)
    return start


def describe(scorecard_path=None, db_path=None):
    """窓の根拠を人間に読める形で返す。分析レポートに必ず添えること。"""
    meta = _rank_meta()
    start = get_oos_start(scorecard_path=scorecard_path, warn_empty=False)
    rank_only = get_oos_start(scorecard_path=scorecard_path, include_scorecard=False,
                              warn_empty=False)
    sc = scorecard_boundary(scorecard_path)
    n_all = window_race_count(start, db_path)
    n_rank = window_race_count(rank_only, db_path)

    def _n(n):
        return "確定レース数は DB が読めず不明" if n is None else f"確定 {n}レース"
    lines = [
        f"OOS評価窓 (印・点数表を採点する時 = 既定): {start} 以降 ({_n(n_all)})",
        f"OOS評価窓 (rank 系だけを採点する時 = include_scorecard=False): {rank_only} 以降 ({_n(n_rank)})",
        f"  rank系 (model_rank/top3/win) の学習境界: race_date <= {meta['train_max_date']}"
        f" — 学習 {meta['train_races']}レース / 検証 {meta['val_races']}レース"
        f" / 特徴量 {meta.get('feature_count', '?')}列",
        f"    分割キー: {meta.get('split_key', 'race_date (正常)')}",
    ]
    if sc:
        p = scorecard_path or _SCORECARD
        with open(p, "rb") as f:
            m = pickle.load(f)
        lines.append(f"  点数表 (印 ◎○▲△△ を決める) の境界: race_date <= {sc}"
                     f" — 区分点の学習 {m.get('pts_fit_from', '')}〜{m.get('pts_fit_until') or m.get('train_until', '?')}"
                     + (f" / 連続項目の当てはめ {m.get('lin_fit_from', '?')}〜{m.get('lin_fit_until')}"
                        if m.get("lin_fit_until") else "")
                     + (f" / V2d 基準表 〜{(m.get('si_variant') or {}).get('cutoff')}"
                        if (m.get("si_variant") or {}).get("cutoff") else ""))
        which = "点数表" if _next_day(sc) >= _next_day(meta["train_max_date"]) else "rank系"
        lines.append(f"  → 既定の窓は遅い方 ({which}) の翌日")
    else:
        lines.append("  点数表 (models/scorecard.pkl) なし — rank 系の境界だけで決めた")
    if n_all == 0:
        lines.append("  ⚠️ 既定の窓に確定レースが0件 — 印・点数表の学習外の評価はできない")
    if 'broken' in str(meta.get('split_key', '')):
        lines.append("  ⚠️ rank系は壊れた分割で学習されている。"
                     "上の窓は混入ゼロを保証する保守的な値。")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    print(describe(sys.argv[1] if len(sys.argv) > 1 else None))
