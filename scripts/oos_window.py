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

以降、backtest は必ずこのモジュール経由で窓を取得すること。
"""
import json
import os

_BOUNDARY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "models", "train_boundary.json")


def get_oos_start(strict=True):
    """OOS 評価窓の開始日 (YYYY-MM-DD)。この日より後のレースだけが真の out-of-sample。

    strict=True (既定): 学習側の最大日付の翌日を返す = 混入ゼロを保証する保守的な窓。
    strict=False: val 側の最小日付 (分割が正しければ同じ、壊れていれば楽観的)。
    """
    if not os.path.exists(_BOUNDARY):
        raise RuntimeError(
            "models/train_boundary.json がありません。fast_train を1度実行するか、"
            "バックテストの評価窓を手で確定させてください。"
            "固定日のハードコードは #141 で禁止されました。")
    with open(_BOUNDARY, encoding="utf-8") as f:
        meta = json.load(f)
    if strict:
        from datetime import datetime, timedelta
        d = datetime.strptime(meta["train_max_date"], "%Y-%m-%d") + timedelta(days=1)
        return d.strftime("%Y-%m-%d")
    return meta["val_min_date"]


def describe():
    """窓の根拠を人間に読める形で返す。分析レポートに必ず添えること。"""
    with open(_BOUNDARY, encoding="utf-8") as f:
        meta = json.load(f)
    start = get_oos_start()
    lines = [
        f"OOS評価窓: race_date > {meta['train_max_date']} (= {start} 以降)",
        f"  学習 {meta['train_races']}レース / 検証 {meta['val_races']}レース"
        f" / 特徴量 {meta.get('feature_count', '?')}列",
        f"  分割キー: {meta.get('split_key', 'race_date (正常)')}",
    ]
    if 'broken' in str(meta.get('split_key', '')):
        lines.append("  ⚠️ このモデルは壊れた分割で学習されている。"
                     "上の窓は混入ゼロを保証する保守的な値。")
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
