"""予測・買い目フラグの単一情報源 (#150).

優先順位: 環境変数 > config/prediction_flags.yml > 呼び出し側の既定値。

なぜ必要か:
  これらのフラグは GitHub Actions の workflow env にしか書かれておらず、
  5本ある予測経路のうち2本にしか設定が無かった。結果、manual_predict や
  ローカル復旧から予測を回すと印が黙って別ロジックになり、
  「同じ日の予想が経路によって違う」状態が起きていた (#150 監査で確定)。
  env を書き忘れても既定が効くように、設定をリポジトリ側に持たせる。
"""
import os

_CFG = None


def _load():
    global _CFG
    if _CFG is not None:
        return _CFG
    _CFG = {}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "config", "prediction_flags.yml")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line or ":" not in line:
                    continue
                k, v = line.split(":", 1)
                _CFG[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    except Exception as e:      # 壊れた設定でパイプラインを止めない
        print(f"⚠️ prediction_flags.yml の読み込みに失敗 ({e}) → 環境変数のみ使用")
    return _CFG


def get(name, default=""):
    """フラグ値を文字列で返す。env が最優先。"""
    v = os.environ.get(name)
    if v is not None and v != "":
        return v
    return _load().get(name, default)


def is_on(name, default="0"):
    return get(name, default) == "1"


def as_float(name, default="0.0"):
    try:
        return float(get(name, default))
    except (TypeError, ValueError):
        return float(default)
