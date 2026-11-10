#!/usr/bin/env python3
"""レース条件ごとの「実測値」基準表を作る (#151)

なぜ必要か
----------
これまで読者に出していたのは「信頼度S(鉄板級)」という **ラベル1個** だった。
検証 (#151) の結果、そのラベルは

  - ◎を選ぶモデル (点数表) と、信頼度を計算するモデル (ML勝率) が別物で
    連動していない (◎が「AI勝率1位」だった割合は 75% → 42% に低下)
  - 1番人気オッズ+頭数+クラス に足しても予測力の増分は AUC +0.0009 (p=0.455)
    = 「1番人気のオッズ」の劣化した言い換えでしかない

と判明した。ラベルを直すのではなく、**そのレース条件で実際に何が起きたかの
実測値** を出す方が、読者 (印を見て自分で買い目を組む人) の役に立つ。

出すもの (すべて実測、モデルの予言ではない)
--------------------------------------------
  1. 印5頭 / 印7頭で3着内3頭が全部揃った割合 → 買い目の点数設計に直結
  2. 三連複の配当中央値 / 1万円超の割合     → 期待値の感覚

条件は **出走頭数** で切る。#151 の検証で、捕捉率を動かしている実体は
頭数であり (能力スコアのシェアは 5/頭数 との相関 +0.741、機械的部分を引くと
AUC 0.5005 = 情報ゼロ)、他のオッズ非依存な候補はどれも頭数を超えなかったため。
「当たり前の方向」ではあるが **大きさは頭で計算できない** ので価値がある。

副産物: 信頼度の閾値を分布から決める
------------------------------------
信頼度ラベル自体は読者には出さなくなるが、投資判定 (should_bet の C/D 遮断)
が内部で使い続ける。#147 で ◎ が点数表由来になった瞬間に ◎表示勝率の分布が
下にずれ、固定閾値 (S≥45/A≥30/B≥20/C≥13) とペアが外れて **D が 11% → 39% に
激増** した (#36/#120 で二度書いた「閾値と分布はペア」の三度目の再発)。
ここで **現行の印ロジックが実際に出している分布の分位** から閾値を作り、
モデルや印ロジックが変わっても自動で追従するようにする。

実行:
    python3 scripts/build_race_baseline.py            # docs/data/race_baseline.json を更新
    python3 scripts/build_race_baseline.py --dry-run  # 書かずに表示だけ
"""
import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from race_utils import is_ml_out_of_domain  # noqa: E402

JST = timezone(timedelta(hours=9))
OUT = os.path.join(ROOT, "docs", "data", "race_baseline.json")

# 出走頭数の区切り。JRA は 5〜18頭。境界は [min, max] の閉区間。
BUCKETS = [
    (0, 10, "10頭以下"),
    (11, 12, "11-12頭"),
    (13, 14, "13-14頭"),
    (15, 16, "15-16頭"),
    (17, 99, "17頭以上"),
]

# 信頼度の目標構成比。#147 (点数表ON) の直前、2026-08 の実分布を採用。
# 「どの程度の割合をS/A/B/C/Dと呼ぶか」は製品の決めごとなので固定し、
# その割合を満たす閾値だけを実分布から毎回引き直す。
# #169: この数字は 2026-08 に **保存された** (= 後段補正の後の) 構成比そのもの
# (同月の補正前は S17/A26/B34/C16/D7)。なので閾値も補正後の最終構成比で較正する。
TARGET_MIX = {"S": 0.18, "A": 0.24, "B": 0.29, "C": 0.18, "D": 0.11}
# 30 は「精度」ではなく「正しい分布に乗る」ことを優先した下限。
# 印ロジックを変えた直後は現行ロジックの標本が数十件しか無いが、
# 旧ロジック混じりの大標本から引くと中心がずれたまま固定される (それが #147 の失敗)。
# 標本が薄い間は分位が粗いだけで、中心は正しい方に寄る。週次で引き直されるので収束する。
MIN_SAMPLE_FOR_THRESHOLDS = 30

# 投資ゲートの目標遮断率 = 2026-05 (0.12/0.30 が設計された時期) の実測 (#156)
_GATE_TARGET = {"top_prob": 0.30, "top3_sum": 0.25}

# #170: 投資ゲートの窓を広げる時の「分散のずれ」安全弁。
# 毎週再学習 (#169) では、金曜〜土曜未明の時点で現モデルの使えるレースは土曜分 (10〜22件) しか
# 無く、毎週「新モデル約3割＋前モデル約7割」の拡張窓から閾値を引く。分散が安定している間は
# 拡張窓の方がノイズが小さいが、再学習でレース内 rank_score の sd が大きく動いた週は #156 の
# 暴れがそのまま起きる。そこで現モデルの行と、窓の中のそれより前の行の「レース内 sd の中央値」の
# 比 (大きい方/小さい方) を見て、限度を超えたら前モデルの行を混ぜない。
#
# 1.4 の根拠 (2026-05〜10 の各土曜を毎週配備で再生、本番DBコピー 11/05):
#   同じモデルのまま週末が替わるだけの揺れ: 最大 1.28 (6/27)、安定期 9/19〜10/31 は 1.01〜1.22
#   遮断率が実害の出るほどずれた分散変化: 最小 1.56〜1.65 (7/04・7/25: 目標 30/25% から
#                                         12〜21pt)、8/08 は 4.3 (遮断 約8割)、9/05 は 8.6 (遮断0%)
#   両者の間 (1.28 と 1.56 の幾何平均 ≈1.41)。見逃しの害 (偏り 12pt 以上) の方が誤検知の害
#   (現モデルだけの窓 n=10〜22 の標本誤差 ±10〜14pt) より大きいので、真ん中よりやや下に置く。
DISPERSION_RATIO_LIMIT = 1.4
# 安全弁が働いた時に、現モデルの行だけで分位を取る下限。2場開催の土曜は使えるレースが
# 10件前後 (9/12=10, 6/06=10)。n=10 の30%分位は遮断率の標準誤差が約±14pt で、分散が
# 動いた週の偏り (目標から 7/25 約-20pt、8/08 約+50pt、9/05 約-30pt) より小さい。これ未満では
# 引かず、拡張窓のまま「ずれを検知したが直せていない」と記録する (JSON の note)。
DISPERSION_MIN_CURRENT = 10
# sd の中央値を比べるのに要る最小レース数 (これ未満は比を出さない = 安全弁も働かない)
DISPERSION_MIN_RACES = 5


def _bucket_of(n):
    for lo, hi, lab in BUCKETS:
        if lo <= n <= hi:
            return lab
    return None


# レースの層。ML の学習範囲外 (未勝利・新馬・障害) は捕捉率も配当も
# 平場とはっきり違う (実測: 15-16頭で 36.4% vs 20.9%) ので必ず分ける。
# 混ぜて出すと、未勝利戦に平場の数字を当ててしまい嘘になる。
GROUPS = ("flat", "maiden")


def _group_of(race_name):
    return "maiden" if is_ml_out_of_domain(race_name or "") else "flat"


# #169: 発走時刻より後に作り直された予測は、そのレースの結果やそれに近いオッズを
# 見ている (look-ahead)。created_at は SQLite の datetime('now') = **UTC** で、
# races.start_time (HH:MM) は **JST** なので +9時間してから比べる。
# 実測: 全1,651R 中 126R が該当 (5/09 の36R は全レース確定後の 20:53 JST に作り直し、
# 完全捕捉 77.8%)。捕捉率の物差しにも閾値の標本にも入れない。
# 発走時刻が無いレースは判定できないので残す (現 DB では0件)。
PRE_START_SQL = ("(g.start_time IS NULL OR g.start_time = '' OR "
                 "datetime(pc.created_at, '+9 hours') < g.race_date || ' ' || g.start_time)")

# #169: 信頼度の後段補正 (confidence.post_adjust) を入れ替えた最初のレース日。
# 金曜 (11/06) 22:30 の前夜予測が最初に新しい補正で走る。この日以降のレースだけで
# 「新しい補正で付いた構成比」と「撤去した過信帯の実額」を測る。
ADJUST_POLICY_SINCE = "2026-11-07"


def _is_new_adjust_logic(race_date, conf_reason):
    """そのレースの信頼度が #169 の新しい補正で付いたか。

    日付で切った上で、デプロイが遅れて旧ロジックで予測された行は旧補正の痕跡で除く:
    「過信帯」「不信騎手」の追記、または閾値だけの等級が C (様子見) なのに信頼騎手で
    上がっている行 (新しい補正は C→B をしない)。
    """
    if (race_date or "") < ADJUST_POLICY_SINCE:
        return False
    cr = conf_reason or ""
    if "過信帯" in cr or "不信騎手" in cr:
        return False
    try:
        from confidence import GRADE_LABELS
        if "信頼騎手" in cr and f"自信度{GRADE_LABELS['C']}" in cr:
            return False
    except Exception:
        pass
    return True


def build_capture(conn):
    """印が3着内をどれだけ捕まえたかの実測 (発走前の予測の印 × 確定着順)。層別。"""
    n_all = conn.execute("""SELECT COUNT(*) FROM predictions_cache pc
                            JOIN races g ON pc.race_id = g.race_id""").fetchone()[0]
    rows = conn.execute(f"""
        SELECT pc.race_id, pc.predictions_json, g.race_name, g.race_date
        FROM predictions_cache pc JOIN races g ON pc.race_id = g.race_id
        WHERE {PRE_START_SQL}
        ORDER BY g.race_date
    """).fetchall()
    n_post_start = n_all - len(rows)
    acc = {g: {lab: {"n": 0, "c5": 0, "c7": 0, "got": 0} for _, _, lab in BUCKETS}
           for g in GROUPS}
    dates = []
    for r in rows:
        grp = _group_of(r["race_name"])
        res = {x["horse_number"]: x["finish_position"] for x in conn.execute(
            "SELECT horse_number, finish_position FROM results WHERE race_id = ?", (r["race_id"],))}
        if not any(v == 1 for v in res.values()):
            continue
        top3 = {h for h, f in res.items() if 0 < f <= 3}
        if len(top3) != 3:            # 同着で4頭以上になる場合は対象外
            continue
        try:
            preds = json.loads(r["predictions_json"] or "[]")
        except (ValueError, TypeError):
            continue
        cap5 = {p["horse_number"] for p in preds if p.get("mark") in ("◎", "○", "▲", "△", "×")}
        if len(cap5) < 4:             # 印が揃っていないレースは除く
            continue
        cap7 = {p["horse_number"] for p in preds if p.get("mark")}
        b = _bucket_of(len(preds))
        if not b:
            continue
        a = acc[grp][b]
        a["n"] += 1
        a["got"] += len(cap5 & top3)
        a["c5"] += 1 if len(cap5 & top3) == 3 else 0
        a["c7"] += 1 if len(cap7 & top3) == 3 else 0
        dates.append(r["race_date"])
    groups = {}
    for g in GROUPS:
        out = []
        for lo, hi, lab in BUCKETS:
            a = acc[g][lab]
            if a["n"] < 20:           # 母数が薄い区分は数字を出さない (誤解を招くため)
                out.append({"label": lab, "min": lo, "max": hi, "n": a["n"]})
                continue
            out.append({
                "label": lab, "min": lo, "max": hi, "n": a["n"],
                "cap5": round(a["c5"] / a["n"], 4),
                "cap7": round(a["c7"] / a["n"], 4),
                "avg_captured": round(a["got"] / a["n"], 3),
            })
        groups[g] = {"buckets": out}
    period = f"{min(dates)}〜{max(dates)}" if dates else "-"
    # #169: 旧 source 欄は「投稿時に凍結した印」と書いていたが、実際に読んでいるのは
    # predictions_cache に残った各レースの最後の予測 (配信していないレースも含む) で、
    # 投稿印サイドカーは 7/11 からしか無い。実態どおりに書く。
    return {"groups": groups, "period": period,
            "excluded_post_start": n_post_start,
            "source": ("predictions_cache に残る各レースの予測の印 (発走前に作られたものだけ。"
                       "配信していないレースも含む) × 確定着順 (平場 / 未勝利・新馬・障害 を分けて集計)")}


def build_payout(conn):
    """三連複の配当実測 (全期間)。捕捉率と同じ層で分ける。"""
    rows = conn.execute("""
        SELECT p.payout_amount a, g.race_name,
               (SELECT COUNT(*) FROM results r WHERE r.race_id = g.race_id) nh
        FROM payouts p JOIN races g ON p.race_id = g.race_id
        WHERE p.bet_type = '三連複' AND p.payout_amount > 0
    """).fetchall()
    acc = {g: {lab: [] for _, _, lab in BUCKETS} for g in GROUPS}
    for x in rows:
        lab = _bucket_of(x["nh"])
        if lab:
            acc[_group_of(x["race_name"])][lab].append(x["a"])
    groups = {}
    for g in GROUPS:
        out = []
        for lo, hi, lab in BUCKETS:
            v = sorted(acc[g][lab])
            if len(v) < 100:
                out.append({"label": lab, "min": lo, "max": hi, "n": len(v)})
                continue
            out.append({
                "label": lab, "min": lo, "max": hi, "n": len(v),
                "trio_median": int(v[len(v) // 2]),
                "trio_over10k": round(sum(1 for x in v if x >= 10000) / len(v), 4),
            })
        groups[g] = {"buckets": out}
    r = conn.execute("""SELECT MIN(g.race_date) a, MAX(g.race_date) b FROM payouts p
                        JOIN races g ON p.race_id = g.race_id WHERE p.bet_type='三連複'""").fetchone()
    return {"groups": groups, "period": f"{r['a']}〜{r['b']}",
            "source": "確定配当 (全レース、平場 / 未勝利・新馬・障害 を分けて集計)"}



def current_model_deployed_at(conn=None):
    """現在出荷されているモデルが配備された時刻を (JST表示用, UTC比較用) で返す。

    #156: 閾値は「そのモデルが出した分布」から引かないと意味がない。weekly_retrain は
    再学習しても再予測しないので、直後に基準表を作ると前モデルの predictions_cache から
    閾値を引くことになる。そこでモデルの配備時刻以降に作られた予測だけを窓にする。

    #161: 旧版は `git log -1 -- models/model_rank.pkl` で配備時刻を取っていたが、
    Actions の checkout は shallow (fetch-depth 1) なので **HEAD コミットの時刻**が返り、
    毎回「配備されたばかり」扱い → 窓がほぼ空 → 全期間 (前モデルの分布) にフォールバック
    していた。しかも created_at (SQLite CURRENT_TIMESTAMP = UTC) を JST 文字列と比べていた。
    結果、閾値 0.0985/0.2608 は現行分布より大幅に低く、9/19・9/20・9/26 の遮断は 0/24。
    → (1) GitHub API でモデルファイルの本当の最終コミットを引く、(2) shallow な git log は
    信用しない、(3) 比較は UTC で行う。
    """
    times = _model_commit_times(1)
    if not times:
        return None, None     # 不明 → 呼び出し側は全期間を使い「窓が合っていない」と明示する
    return _deploy_pair(times[0])


def previous_model_deployed_at(conn=None):
    """現モデルの**1つ前**のモデルが配備された時刻を (JST表示用, UTC比較用) で返す (#170)。

    current_model_deployed_at と同じ取り方 (GitHub API → shallow でない時だけ git log) で
    直近2件を引き、2件目を返す。取れない時は (None, None) = 呼び出し側は従来の挙動。
    投資ゲートの窓を広げる範囲を「直前のモデルの配備以降」に限り、2世代以上前のモデルの
    予測を混ぜないために使う。
    """
    times = _model_commit_times(2)
    if len(times) < 2:
        return None, None
    return _deploy_pair(times[1])


def _deploy_pair(utc):
    jst = utc.astimezone(timezone(timedelta(hours=9)))
    return (jst.strftime("%Y-%m-%d %H:%M:%S"),
            utc.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))


def _model_commit_times(limit):
    """models/model_rank.pkl を変えたコミットの時刻を新しい順に最大 limit 件 (aware datetime)。

    #161 の取り方そのまま: GitHub API が取れればそれ、駄目なら shallow でない時だけ git log。
    #170: current_model_deployed_at (1件) と previous_model_deployed_at (2件) で共有する。
    """
    import subprocess, urllib.request
    rel = "models/model_rank.pkl"
    times = []
    repo = os.environ.get("GITHUB_REPOSITORY", "daiki0726m-oss/equinox-lab")
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/commits?path={rel}&per_page={limit}",
            headers={"Accept": "application/vnd.github+json",
                     **({"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"}
                        if os.environ.get("GITHUB_TOKEN") else {})})
        with urllib.request.urlopen(req, timeout=20) as r:
            commits = json.load(r)[:limit]
        times = [datetime.fromisoformat(c["commit"]["committer"]["date"].replace("Z", "+00:00"))
                 for c in commits]
        if not times:
            raise ValueError("empty")
    except Exception:
        times = []
        try:
            shallow = subprocess.run(["git", "rev-parse", "--is-shallow-repository"], cwd=ROOT,
                                     capture_output=True, text=True, timeout=30).stdout.strip()
            if shallow == "false":
                out = subprocess.run(["git", "log", f"-{limit}", "--format=%cI", "--", rel],
                                     cwd=ROOT, capture_output=True, text=True, timeout=30)
                if out.returncode == 0 and out.stdout.strip():
                    times = [datetime.fromisoformat(x.strip())
                             for x in out.stdout.strip().splitlines() if x.strip()]
        except Exception:
            times = []
    return times


def model_uncommitted(rel="models/model_rank.pkl"):
    """作業ツリーの models/model_rank.pkl が HEAD と違う (= 再学習したが未 commit) か。

    #169: weekly_retrain は「再学習 → 基準表 → commit」の順に動く。基準表を作る時点では
    新モデルが origin に無いので、current_model_deployed_at() は**前のモデル**の時刻を返し、
    前モデルの予測が30件以上あると window_matches_model=true と誤記録していた
    (11/02・10/19・9/28 の3回。誤っていたのは記録だけで、閾値の値はフラグが正しくても同じ)。
    git status は index に記録されたハッシュと作業ファイルを比べるだけなので、
    shallow / 部分クローン (blob:none) でも過去の blob を取りに行かない。
    判定できない時 (git が無い等) は False (従来どおり)。

    #169 review: git status だけだと「commit 済み・未 push」を見逃す (再学習 → commit →
    基準表 → push の順に並べ替えた瞬間に黙って true に戻る)。そこで HEAD と upstream の
    **ツリー上の OID** も比べる (rev-parse <rev>:<path> は tree を読むだけで blob を取りに
    行かない)。ただし upstream が HEAD の祖先の時だけ見る — ローカルが古く origin に新しい
    モデルがある (= 次の予測は origin のモデルが出す) 場合まで「未配備」と誤記録しないため。
    shallow で祖先が辿れない時は判定を諦める (False、従来どおり)。
    """
    import subprocess

    def _git(*a):
        return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, timeout=30)

    try:
        out = _git("status", "--porcelain", "--", rel)
        if out.returncode == 0 and out.stdout.strip():
            return True
        for up in ("@{upstream}", "origin/main"):
            if _git("rev-parse", "--verify", "-q", up).returncode != 0:
                continue
            if _git("merge-base", "--is-ancestor", up, "HEAD").returncode != 0:
                return False
            a = _git("rev-parse", "-q", "--verify", f"HEAD:{rel}")
            b = _git("rev-parse", "-q", "--verify", f"{up}:{rel}")
            return a.returncode == 0 and (b.returncode != 0
                                          or a.stdout.strip() != b.stdout.strip())
        return False
    except Exception:
        return False


def _gate_samples(rows):
    """predictions_cache の行から、意思決定チャネル (温度1 softmax = ml/model.py の
    pred_win_norm) の ◎勝率と上位3頭合計を復元する。障害・未勝利・新馬は除く。"""
    tops, sums = [], []
    for r in rows:
        if is_ml_out_of_domain(r["race_name"] or ""):
            continue
        try:
            preds = json.loads(r["predictions_json"] or "[]")
        except (ValueError, TypeError):
            continue
        rs = [p.get("rank_score") for p in preds if p.get("rank_score") is not None]
        if len(rs) < 3:
            continue
        mx = max(rs)
        ex = [math.exp(x - mx) for x in rs]
        tot = sum(ex) or 1.0
        pr = sorted((v / tot for v in ex), reverse=True)
        tops.append(pr[0])
        sums.append(sum(pr[:3]))
    return tops, sums


def _rank_sd_median(rows):
    """レース内 rank_score の標準偏差 (母標準偏差) の中央値と、数えたレース数 (#170)。
    対象は _gate_samples と同じ (障害・未勝利・新馬を除き、rank_score が3頭以上)。"""
    sds = []
    for r in rows:
        if is_ml_out_of_domain(r["race_name"] or ""):
            continue
        try:
            preds = json.loads(r["predictions_json"] or "[]")
        except (ValueError, TypeError):
            continue
        rs = [p.get("rank_score") for p in preds if p.get("rank_score") is not None]
        if len(rs) < 3:
            continue
        m = sum(rs) / len(rs)
        sds.append(math.sqrt(sum((x - m) ** 2 for x in rs) / len(rs)))
    if not sds:
        return None, 0
    sds.sort()
    k = len(sds)
    med = sds[k // 2] if k % 2 else (sds[k // 2 - 1] + sds[k // 2]) / 2
    return med, k


def _quantile_thresholds(tops, sums):
    """目標遮断率 (_GATE_TARGET) を弾く分位点 (#156)。"""
    t, s = sorted(tops), sorted(sums)
    return {
        "top_prob": round(t[int(len(t) * _GATE_TARGET["top_prob"])], 4),
        "top3_sum": round(s[int(len(s) * _GATE_TARGET["top3_sum"])], 4),
    }


def _block_rates(tops, sums, th):
    """閾値 th で弾かれる割合 (should_bet_race と同じく `<` で判定) (#170)。"""
    n = len(tops)
    if not n:
        return {"top_prob": None, "top3_sum": None, "either": None}
    bt = [x < th["top_prob"] for x in tops]
    bs = [y < th["top3_sum"] for y in sums]
    return {"top_prob": round(sum(bt) / n, 4), "top3_sum": round(sum(bs) / n, 4),
            "either": round(sum(a or b for a, b in zip(bt, bs)) / n, 4)}


def build_bet_gate_thresholds(conn):
    """投資ゲートの「混戦すぎるレースを弾く」閾値を、現行モデルの分布から引く (#156)。

    なぜ固定値ではダメか
    --------------------
    `should_bet_race` は `top_prob < 0.12` / `top3_sum < 0.30` という**絶対値**で
    弾いている。この値は 2026-05 の分布 (#16/#25) で決められたが、意思決定チャネル
    `pred_win_norm` は生 softmax のままで (#120 は表示チャネルだけ z-score 化した)、
    rank_score のレース内 sd は再学習のたびに大きく動く。実測:

        2026-05 sd 0.493 → 遮断 30.7%
        2026-06 sd 0.219 → 遮断 67.8%
        2026-07 sd 0.347 → 遮断 29.2%
        2026-08 sd 0.115 → 遮断 70.1%     ← 7割のレースで投資が止まっていた
        2026-09 sd 0.869 → 遮断  1.7%     ← ほぼ素通り

    つまり「どれだけ投資するか」が、予測の中身でなく**再学習の引き**で決まっていた。
    #36/#120/#147 が三度書いた「閾値と分布はペア」が、投資ゲートにだけ未適用だった。

    ここでは 2026-05 (0.12/0.30 が設計された時期) の遮断率を目標として、
    現行モデルの分布から同じ割合を弾く分位点を引く。ROI を上げるための変更ではない
    — 週ごとに投資額が7倍動くのを止めるための変更 (#140 の「分散の削減」)。

    #170: 30件未満で窓を広げる時の2つの安全策
    ------------------------------------------
    毎週再学習 (#169) では、金曜〜土曜未明の時点で現モデルの使えるレースは土曜分しか無く、
    毎週この拡張窓に落ちる。安定期 (レース内 sd の中央値 0.73〜0.82) は拡張窓の方がノイズが
    小さいが、再学習で分散が動いた週は #156 の暴れがそのまま起きる (8/08 sd 0.53→0.12 で
    遮断77%、9/05 0.11→0.91 で遮断0%)。そこで
      (1) 広げる範囲を「直前のモデルの配備以降」に限る (2世代以上前を混ぜない。毎週再学習の
          2場開催の週末は直近120行が2週末前まで届き、9/12 は8月の平坦なモデルまで混ざって遮断9%)
      (2) 現モデルの行とそれより前の行で「レース内 sd の中央値」を比べ、比が
          DISPERSION_RATIO_LIMIT を超えたら前モデルの行を混ぜず現モデルの行だけで分位を取る。
          超えなければ拡張窓のまま (安定期の挙動は変えない)。
    窓の内訳は JSON の window_rule / n_current / n_older / sd_median_* / dispersion_ratio に残す。
    """
    # 2026-05 の実測遮断率 = 閾値が意図した水準
    TARGET = _GATE_TARGET
    FALLBACK = {"top_prob": 0.12, "top3_sum": 0.30}
    # #156: 現モデルが出した予測だけを窓にする。足りなければ全期間に広げ、
    # 「前モデルの分布から引いた」ことをログと JSON に明示する。
    since, since_utc = current_model_deployed_at(conn)
    fresh = bool(since_utc)
    # #169: 再学習直後 (新モデル未 commit) は、窓の起点が前モデルの配備時刻になる。
    # 閾値の引き方は変えない (新モデルの予測はまだ1件も無いので、どう引いても前モデルの
    # 分布になる = #161 が認めた代用策) が、「一致」とは記録しない。
    retrained = model_uncommitted()
    if retrained:
        fresh = False
    rows = []
    if since_utc:
        rows = list(conn.execute(f"""
            SELECT pc.predictions_json, g.race_name, pc.created_at FROM predictions_cache pc
            JOIN races g ON pc.race_id = g.race_id
            WHERE pc.created_at >= ? AND {PRE_START_SQL}
            ORDER BY g.race_date DESC LIMIT 400
        """, (since_utc,)))   # created_at は UTC (SQLite CURRENT_TIMESTAMP)
    cur_rows = rows   # #170: 現モデル (配備時刻以降) の行。分散のずれの比較に使う
    tops, sums = _gate_samples(rows)
    # #170: 窓の診断 (既存のキーは変えず足すだけ)。window_rule:
    #   current_model                  … 現モデルだけで30件以上 (従来どおり)
    #   since_prev_model               … 30件未満 → 直前のモデルの配備以降 (最大120行) に広げた
    #   recent120                      … 30件未満 → 直近120行に広げた (従来の挙動。直前の配備時刻が
    #                                     取れない / その窓も30件未満 / 配備時刻自体が不明)
    #   current_model_dispersion_shift … 広げた窓と現モデルで分散がずれた → 現モデルの行だけ
    diag = {"window_rule": "current_model" if since_utc else "recent120",
            "n_current": len(tops) if since_utc else None, "n_older": None,
            "sd_median_current": None, "sd_median_older": None,
            "dispersion_ratio": None, "dispersion_ratio_limit": DISPERSION_RATIO_LIMIT,
            "prev_model_deployed_at": None}
    # #161: 標本数は **使えるレース数** (障害・未勝利・新馬を除いた後) で判定する。
    # 旧版は除外前の行数で判定したため、30〜55行の窓 (= 再学習直後の週末1日分) では
    # 除外後に30を割って固定値 0.12/0.30 に落ちていた。足りなければ全期間に広げ、
    # 「前モデルの分布を含む」と明示する (window_matches_model=False)。
    # 広げる範囲は **直近120レース** (使えるのは約60)。旧版の 400 は 2〜3世代前のモデル
    # (8月は sd 0.115 と平坦) まで混ぜるので、再学習直後の閾値が 0.1001/0.2613 =
    # ほぼ素通りに戻っていた (#161 review)。直前のモデルが新モデルに最も近い手掛かり。
    widened = False
    shift = False
    if len(tops) < MIN_SAMPLE_FOR_THRESHOLDS:
        fresh = False
        widened = True
        rows = []
        # #170 (1): 直前のモデルの配備時刻が取れれば、その時刻以降 (最大120行) に限る。
        # 毎週再学習では「先週末 (前モデル) + 土曜 (現モデル)」= 3場で約60、2場で約45 の使えるレース。
        # それでも30件未満 (配備が続いて前モデルの予測がほぼ無い等) なら従来の直近120行に戻す。
        if since_utc:
            prev, prev_utc = previous_model_deployed_at(conn)
            if prev_utc and prev_utc < since_utc:
                diag["prev_model_deployed_at"] = prev
                rows = list(conn.execute(f"""
                    SELECT pc.predictions_json, g.race_name, pc.created_at FROM predictions_cache pc
                    JOIN races g ON pc.race_id = g.race_id
                    WHERE pc.created_at >= ? AND {PRE_START_SQL}
                    ORDER BY g.race_date DESC, pc.created_at DESC LIMIT 120
                """, (prev_utc,)))
                if len(_gate_samples(rows)[0]) >= MIN_SAMPLE_FOR_THRESHOLDS:
                    diag["window_rule"] = "since_prev_model"
                else:
                    rows = []
        if not rows:
            diag["window_rule"] = "recent120"
            rows = list(conn.execute(f"""
                SELECT pc.predictions_json, g.race_name, pc.created_at FROM predictions_cache pc
                JOIN races g ON pc.race_id = g.race_id
                WHERE {PRE_START_SQL}
                ORDER BY g.race_date DESC, pc.created_at DESC LIMIT 120
            """))
        tops, sums = _gate_samples(rows)
        # #170 (2): 分散のずれの安全弁。現モデルの行と、広げた窓のうち配備時刻より前の行で
        # レース内 sd の中央値を比べる (配備時刻が不明なら分けられないので見ない)。
        if since_utc:
            older = [r for r in rows if (r["created_at"] or "") < since_utc]
            sd_cur, k_cur = _rank_sd_median(cur_rows)
            sd_old, k_old = _rank_sd_median(older)
            diag["n_older"] = k_old
            diag["sd_median_current"] = round(sd_cur, 4) if sd_cur is not None else None
            diag["sd_median_older"] = round(sd_old, 4) if sd_old is not None else None
            if k_cur >= DISPERSION_MIN_RACES and k_old >= DISPERSION_MIN_RACES:
                ratio = max(sd_cur, sd_old) / max(min(sd_cur, sd_old), 1e-9)
                diag["dispersion_ratio"] = round(ratio, 3)
                if ratio > DISPERSION_RATIO_LIMIT:
                    shift = True
                    if diag["n_current"] >= DISPERSION_MIN_CURRENT:
                        tops, sums = _gate_samples(cur_rows)
                        diag["window_rule"] = "current_model_dispersion_shift"
                        widened = False
                        fresh = not retrained
                    else:
                        diag["note"] = (f"分散のずれ (比 {ratio:.2f} > {DISPERSION_RATIO_LIMIT}) を検知したが、"
                                        f"現モデルの使えるレースが {diag['n_current']} 件 "
                                        f"(< {DISPERSION_MIN_CURRENT}) で現モデルだけでは引けない → 拡張窓のまま")

    if len(tops) < MIN_SAMPLE_FOR_THRESHOLDS and diag["window_rule"] != "current_model_dispersion_shift":
        print(f"🎚 投資ゲート: 標本 {len(tops)} 件は不足 → 固定値 {FALLBACK} を維持")
        return {"available": False, "n": len(tops), "thresholds": FALLBACK, **diag}

    if retrained:
        print(f"   ⚠️ 再学習した新モデルが未 commit。窓の起点 {since or '不明'} は前のモデルの配備時刻で、"
              f"閾値は前モデルの予測分布から引いています (window_matches_model=False)")
    elif widened and diag["window_rule"] == "since_prev_model":
        print(f"   ⚠️ 現モデル ({since}) 以降の使えるレースが {MIN_SAMPLE_FOR_THRESHOLDS} 件未満。"
              f"直前のモデル ({diag['prev_model_deployed_at']}) の配備以降の最大120レースから引いています "
              f"(window_matches_model=False)")
    elif widened:
        print(f"   ⚠️ 現モデル ({since or '配備時刻不明'}) 以降の使えるレースが "
              f"{MIN_SAMPLE_FOR_THRESHOLDS} 件未満。直前のモデルを含む直近120レースから引いています "
              f"(window_matches_model=False)")
    if diag["sd_median_current"] is not None or diag["sd_median_older"] is not None:
        _f = lambda x: f"{x:.3f}" if x is not None else "-"
        print(f"   窓の内訳 [{diag['window_rule']}]: 現モデル {diag['n_current']}R (sd中央値 "
              f"{_f(diag['sd_median_current'])}) / それより前 {diag['n_older']}R (sd中央値 "
              f"{_f(diag['sd_median_older'])}) / 比 {_f(diag['dispersion_ratio'])} "
              f"(限度 {DISPERSION_RATIO_LIMIT})")
    if diag["window_rule"] == "current_model_dispersion_shift":
        print(f"   ⚠️ 分散のずれを検知 (比 {diag['dispersion_ratio']:.2f} > {DISPERSION_RATIO_LIMIT}) → "
              f"前のモデルの行を混ぜず、現モデルの {diag['n_current']} レースだけで引いています "
              f"(30件未満なので分位は粗い)")
    elif shift:
        print(f"   ⚠️ {diag['note']}。閾値が現モデルの分布とずれている可能性がある")
    th = _quantile_thresholds(tops, sums)
    print(f"🎚 投資ゲートの閾値 (n={len(tops)}): "
          f"top_prob≥{th['top_prob']:.3f} / top3_sum≥{th['top3_sum']:.3f} "
          f"(目標遮断率 {TARGET['top_prob']:.0%}/{TARGET['top3_sum']:.0%})")
    return {"available": True, "n": len(tops), "target_block_rate": TARGET,
            "model_deployed_at": since, "window_matches_model": fresh,
            "model_retrained_uncommitted": retrained,   # #169
            "thresholds": th,
            **diag}   # #170


_GRADES = ("S", "A", "B", "C", "D")


def _grade_with(th, w):
    """race_baseline.grade と同じ判定 (w >= 閾値で上の等級)。"""
    for g in ("S", "A", "B", "C"):
        if w >= th[g]:
            return g
    return "D"


def _final_grades(samples, th):
    """閾値判定 → 後段補正 (confidence.post_adjust) まで通した最終等級。
    predict.py と同じ関数を使うので、補正を足し外ししても較正が自動で追従する (#169)。"""
    from confidence import post_adjust
    return [post_adjust(_grade_with(th, w), jt)[0] for w, jt in samples]


def _mix(grades):
    n = len(grades) or 1
    return {g: round(sum(1 for x in grades if x == g) / n, 4) for g in _GRADES}


def _calibrate_after_adjust(samples, th0):
    """補正後の構成比が TARGET_MIX になる閾値を探す (#169)。

    補正後に上位 k 等級 (S..k) に入る件数は、閾値 th_k について単調非増加
    (th_k を上げると等級 k の馬が k+1 に落ちるか、補正で戻っても k に留まるだけ)。
    下の等級から順に、累積の目標件数に最も近くなる th_k を候補 (標本の値) から選ぶ。
    補正が「1段上げ」だけなら1巡で厳密に決まるが、将来「下げる」補正が入っても
    収束するよう、変化が無くなるまで繰り返す。
    """
    n = len(samples)
    cands = sorted({w for w, _ in samples} | {round(max(w for w, _ in samples) + 0.1, 1)})
    th = dict(th0)
    for _ in range(10):
        prev = dict(th)
        cum_target = 0.0
        targets = {}
        for g in ("S", "A", "B", "C"):
            cum_target += TARGET_MIX[g]
            targets[g] = cum_target * n
        lower = None                       # 1段下の等級の閾値 (C は下限なし)
        for g in ("C", "B", "A", "S"):
            k = _GRADES.index(g)
            best, best_err = th[g], None
            for c in cands:
                if lower is not None and c <= lower:
                    continue
                t = dict(th); t[g] = c
                got = sum(1 for x in _final_grades(samples, t) if _GRADES.index(x) <= k)
                err = abs(got - targets[g])
                if best_err is None or err < best_err:
                    best, best_err = c, err
            th[g] = round(best, 1)
            lower = th[g]
        if th == prev:
            break
    for a, b in (("S", "A"), ("A", "B"), ("B", "C")):   # 単調性の保証 (標本が薄いと同値が並ぶ)
        if th[a] <= th[b]:
            th[a] = round(th[b] + 0.1, 1)
    return th


def build_confidence_thresholds(conn):
    """現行の印ロジックが出している ◎表示勝率の分布から、目標構成比を満たす閾値を引く。

    固定閾値は印ロジックやモデルが変わるたびに陳腐化する (#36/#120/#147)。
    分位で決めれば、次に印の選び方を変えても構成比は保たれる。

    #169: 目標構成比 (TARGET_MIX) は 2026-08 の **補正後** の分布なのに、旧版は
    **補正前** の◎表示勝率に分位を当てていた。その後で predict.py が信頼騎手+1 /
    過信帯-1 を足すため、保存される信頼度は目標から崩れ、平場の C/D は目標29% に対し
    41-44% (9/12〜11/01) になっていた。補正 (confidence.post_adjust) を込みにした
    **最終構成比** が目標になるように閾値を較正し、補正前・補正後・実際に保存された
    構成比を JSON に残す (次に崩れた時に週次の基準表で気づけるように)。
    """
    # 現行ロジック = 点数表 (sc_points が入っている) のレースを優先
    samples, used, realized = [], None, []
    for cond, label in (("AND pc.predictions_json LIKE '%\"sc_points\"%'", "点数表 (現行)"),
                        ("", "直近全体 (点数表以前を含む)")):
        samples, realized, realized_new, dates = [], [], [], []
        for r in conn.execute(f"""
            SELECT pc.predictions_json, pc.confidence, pc.conf_reason, g.race_name, g.race_date
            FROM predictions_cache pc
            JOIN races g ON pc.race_id = g.race_id
            WHERE {PRE_START_SQL} {cond} ORDER BY g.race_date DESC LIMIT 400
        """):
            if is_ml_out_of_domain(r["race_name"] or ""):
                continue
            try:
                preds = json.loads(r["predictions_json"] or "[]")
            except (ValueError, TypeError):
                continue
            ax = next((p for p in preds if p.get("mark") == "◎"), None)
            if not ax:
                continue
            w = ax.get("pred_win_display_pct")
            # sc_points 条件では「点数表が実際に効いた」レースだけを見る
            if cond and not any((p.get("sc_points") or 0) for p in preds):
                continue
            if w:
                # 補正の入力は ◎ の jockey_trust (予測時点の人気で計算済み。
                # refresh_odds は odds/popularity しか書き換えないので凍結されている)
                samples.append((float(w), ax.get("jockey_trust", 0) or 0))
                if r["confidence"] in _GRADES:
                    realized.append(r["confidence"])
                    if _is_new_adjust_logic(r["race_date"], r["conf_reason"]):
                        realized_new.append(r["confidence"])
                dates.append(r["race_date"])
        used = label
        if len(samples) >= MIN_SAMPLE_FOR_THRESHOLDS:
            break
    if len(samples) < MIN_SAMPLE_FOR_THRESHOLDS:
        return {"available": False, "n": len(samples),
                "note": f"標本 {len(samples)} 件では閾値を引けない (最低 {MIN_SAMPLE_FOR_THRESHOLDS} 件)"}
    ws = sorted(w for w, _ in samples)
    n = len(ws)
    # 補正前の分位 (旧方式) を初期値にする
    th0, cum = {}, 0.0
    for g in ("S", "A", "B", "C"):
        cum += TARGET_MIX[g]
        idx = max(0, min(n - 1, int(round((1.0 - cum) * n))))
        th0[g] = round(ws[idx], 1)
    th = _calibrate_after_adjust(samples, th0)
    pre = _mix([_grade_with(th, w) for w, _ in samples])
    post = _mix(_final_grades(samples, th))
    from confidence import post_adjust
    n_adj = sum(1 for w, jt in samples if post_adjust(_grade_with(th, w), jt)[1])
    return {"available": True, "n": n, "sample": used,
            "sample_period": f"{min(dates)}〜{max(dates)}",
            "target_mix": TARGET_MIX, "thresholds": th,
            "calibrated_on": "補正後 (confidence.post_adjust 込み) の最終構成比",
            "mix_before_adjust": pre, "mix_after_adjust": post,
            "adjusted_share": round(n_adj / n, 4),
            # 実際に保存された信頼度 = **各予測の時点の閾値・補正で付いたもの** (#169 review:
            # 旧版は「旧閾値・旧補正」と直書きしていたが、金曜の前夜予測から新しいロジックの値に
            # 置き換わっていくので名前と中身がずれる #163)。目標から崩れていないかの監視用。
            # 新しい補正 (ADJUST_POLICY_SINCE 以降) で付いた分だけを別に出す — 較正が効いて
            # いるかはこちらで見る (標本が貯まるまで n は小さい)。
            "realized_mix": _mix(realized), "realized_n": len(realized),
            "realized_note": "各予測の時点の閾値・補正で付いた信頼度 (predictions_cache.confidence)",
            "realized_new_logic_since": ADJUST_POLICY_SINCE,
            "realized_new_logic_mix": _mix(realized_new), "realized_new_logic_n": len(realized_new)}


# #169 review: 撤去した「過信帯 -1」(◎の予測時実オッズ 5.0〜7.9倍で1段下げ) は、
# 再検証で区間がゼロをまたいだので外したが、点推定はどれも「残す側」に寄っていた。
# 撤去でこの帯に新しくお金が乗るので、**実際に金額を乗せた買い目** で毎週測る
# (監視する仕組みの無い「継続監視」は約束にならない)。基準表は weekly_retrain と
# 金曜の前夜予測の後に毎回作り直されるので、ここに置けば必ず更新される。
OVC_BAND = (5.0, 8.0)
WATCH_MIN_N = 30          # これ未満では判定しない (「サンプル不足」と出す)


def _funded_lines(all_bets_json):
    """predictions_cache.all_bets_json → weekly_scorecard.score_actual が読む形。
    (#152: 正本は投稿時点で凍結された all_bets。amount==0 は表示だけのライン)"""
    try:
        ab = json.loads(all_bets_json or "{}")
    except (ValueError, TypeError):
        return {}
    if not isinstance(ab, dict):
        return {}
    out = {}
    for bt, ls in ab.items():
        acc = []
        for b in ls or []:
            nums = b.get("horse_numbers") or []
            if nums:
                acc.append({"nums": [int(x) for x in nums], "amount": int(b.get("amount") or 0),
                            "honor": bool(b.get("honor"))})
        if acc:
            out[bt] = acc
    return out


def _roi_diff_ci(a, b, n_boot=2000, seed=169):
    """2群 (各要素は (spend, ret)) の回収率の差 a−b と、レース単位 bootstrap の 95%CI。"""
    import random
    roi = lambda xs: sum(r for _, r in xs) / max(1, sum(s for s, _ in xs))
    est = roi(a) - roi(b)
    rng = random.Random(seed)
    bs = sorted(roi([rng.choice(a) for _ in a]) - roi([rng.choice(b) for _ in b])
                for _ in range(n_boot))
    return est, bs[int(0.025 * n_boot)], bs[int(0.975 * n_boot) - 1]


def build_adjust_watch(conn):
    """撤去した過信帯 (◎5.0〜7.9倍) に乗ったお金の実額回収を、残りの投資レースと比べる。

    母集団: ADJUST_POLICY_SINCE 以降に新しい補正で信頼度が付き、発走前に予測され、
    結果が確定し、1円以上張った平場のレース。帯の判定は conf_reason の「オッズX倍」
    (= 予測時点の◎の単勝オッズ、refresh_odds に上書きされない #99) で、レース当日 (JST)
    に作られた予測だけを「実オッズあり」とみなす (9/05〜11/01 の 552R は全件当日予測で、
    帯の判定は予測直後に export された JSON の◎オッズと 136/136 一致)。
    判定規則 (#169 と同じ): 帯の n≥30 かつ (帯 − 残り) の 95%CI の上端 < 0 なら
    「過信帯を should_bet 側の規則として戻すか検討」を出す。
    """
    import re
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from weekly_scorecard import score_actual, payouts_of   # #152 と同じ実額の採点
    band, rest, band_days = [], [], {}
    rows = conn.execute(f"""
        SELECT pc.race_id, pc.all_bets_json, pc.conf_reason, pc.created_at,
               g.race_name, g.race_date
        FROM predictions_cache pc JOIN races g ON pc.race_id = g.race_id
        WHERE g.race_date >= ? AND {PRE_START_SQL}
        ORDER BY g.race_date
    """, (ADJUST_POLICY_SINCE,)).fetchall()
    for r in rows:
        if is_ml_out_of_domain(r["race_name"] or ""):
            continue
        if not _is_new_adjust_logic(r["race_date"], r["conf_reason"]):
            continue
        if not conn.execute("SELECT 1 FROM results WHERE race_id=? AND finish_position=1",
                            (r["race_id"],)).fetchone():
            continue                      # 未確定・中止はまだ採点しない
        sc = score_actual(payouts_of(conn, r["race_id"]), _funded_lines(r["all_bets_json"]))
        if sc["spend"] <= 0:
            continue
        m = re.search(r"オッズ([\d.]+)倍 /", r["conf_reason"] or "")
        odds = float(m.group(1)) if m else 0.0
        try:
            made = (datetime.fromisoformat(r["created_at"]) + timedelta(hours=9)).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            made = ""
        rec = (sc["spend"], sc["ret"])
        if made == r["race_date"] and OVC_BAND[0] <= odds < OVC_BAND[1]:
            band.append(rec)
            band_days[r["race_date"]] = band_days.get(r["race_date"], 0.0) + sc["ret"]
        else:
            rest.append(rec)
    roi = lambda xs: (round(sum(x for _, x in xs) / sum(s for s, _ in xs), 4)
                      if xs and sum(s for s, _ in xs) else None)
    out = {"since": ADJUST_POLICY_SINCE, "band_odds": list(OVC_BAND),
           "population": "新しい補正で信頼度が付き、1円以上張った平場の確定レース (実額)",
           "band": {"n": len(band), "spend": sum(s for s, _ in band),
                    "ret": round(sum(x for _, x in band)), "roi": roi(band)},
           "rest": {"n": len(rest), "spend": sum(s for s, _ in rest),
                    "ret": round(sum(x for _, x in rest)), "roi": roi(rest)},
           "rule": f"帯の n≥{WATCH_MIN_N} かつ (帯−残り) の95%CI上端<0 で要レビュー"}
    if band and rest and len(band) >= 5:
        est, lo, hi = _roi_diff_ci(band, rest)
        tot = sum(band_days.values())
        out["diff"] = {"est": round(est, 4), "ci95": [round(lo, 4), round(hi, 4)],
                       # 単日支配 (#26): 帯の回収のうち最良の1日が占める割合
                       "top_day_share_of_band_return": round(max(band_days.values()) / tot, 4)
                       if tot else None}
    if len(band) < WATCH_MIN_N:
        out["status"] = f"サンプル不足 (帯 n={len(band)} < {WATCH_MIN_N})"
    elif out.get("diff", {}).get("ci95", [0, 0])[1] < 0:
        out["status"] = "要レビュー: 帯の回収が残りより低い (95%CI がゼロ未満)。過信帯を should_bet 側の規則として戻すか検討"
    else:
        out["status"] = "区別できない (撤去を維持)"
    return out


def _final_rebuild_jst():
    """開催当日の最後の作り直しの開始時刻 (predict.FINAL_REBUILD_JST #173)。読めなければ None。
    predict の import はモデル周りまで読む (数秒) ので、土曜の 9:30 前の判定でだけ呼ぶ。"""
    try:
        import contextlib
        # import 時の出力を stdout (基準表の表示) に混ぜない
        with contextlib.redirect_stdout(sys.stderr):
            from predict import FINAL_REBUILD_JST
        return tuple(FINAL_REBUILD_JST)
    except Exception as e:
        print(f"⚠️ 当日の最後の作り直しの時刻 (predict.FINAL_REBUILD_JST) を読めない "
              f"({type(e).__name__}: {e})")
        return None


def _weekend_gate_hold(now=None):
    """週末の途中か (= 見送りの基準になる閾値を動かしてはいけないか) を JST の now で判定する (#173)。
    戻り値 (据え置くか, 理由)。

    - 日曜                                   … 土曜の予想は確定済み (従来どおり #161)
    - 土曜で当日の最後の作り直し (9:00) 以降 … 土曜の印と見送り (should_bet) は 9:00 の odds_flash
      (それが作り直さなかった日は遅れて来た auto_post_x の predict) が、その run の起動時に読んだ閾値で
      作り直し、9:30 に確定する。9:00 以降に引き直した閾値を push すると、その作り直しが読む版と
      ずれたり、日曜だけ別の基準になる (#161)。確定 (9:30) を境界にすると 9:00〜9:30 に動いた回が
      引き直してしまう。#173 で土曜の夕方〜夜に日曜分を作る回を足したので、その回は全部ここに入る
    - 土曜で 9:00 を読めない                 … 据え置く側 (9:00 前に据え置いても、前夜の値を使うだけ)
    - 土曜の 9:00 前 (金曜夜の回が土曜未明に遅れて動いた等)・金曜以前 … 引き直してよい
      (土曜の 9:00 の作り直しが新しい閾値で土曜分を揃える)
    「確定済みか」は race_utils.prediction_locked_for (#172)、9:00 は predict.FINAL_REBUILD_JST を
    正とする (時刻を書き下さない)。weekend_prefetch の生成の step も同じ2つで当日分を外す。
    月曜の開催 (祝日・順延) は見ない: weekend_prefetch は月曜を対象にせず、weekly_retrain は
    開催日の月曜は再学習ごと延期する (retrain_decision #169)。
    """
    from race_utils import prediction_locked_for, PREDICTION_LOCK_LABEL
    if now is None:
        now = datetime.now(JST)
    wd = now.weekday()   # 月=0 … 土=5, 日=6
    if wd == 6:
        return True, "日曜 = 土曜の予想は確定済み"
    if wd == 5:
        if prediction_locked_for(now.strftime("%Y%m%d"), now):
            return True, f"土曜の予想の確定 ({PREDICTION_LOCK_LABEL}) 後"
        fr = _final_rebuild_jst()
        if fr is None:
            return True, "土曜で、当日の最後の作り直しの時刻を読めない → 据え置く側"
        label = f"{fr[0]}:{fr[1]:02d}"
        if (now.hour, now.minute) >= fr:
            return True, f"土曜の最後の作り直し ({label}) 以降"
        return False, f"土曜の最後の作り直し ({label}) 前"
    return False, "週末の途中ではない"


def _keep_thresholds_requested(now=None):
    """KEEP_BET_GATE を読む (#161 / #173)。戻り値 (据え置くか, 理由 or None)。

    据え置くのは見送りの基準になる閾値 = 投資ゲート (bet_gate) と信頼度 (confidence)。
    変数名は #161 のまま (信頼度は #173 で加えた)。main() でこれを**1回だけ**呼び、
    両方に同じ判定を渡す (別々に時刻を見ると、9:00 をまたいで片方だけ引き直しうる)。

    1    … 無条件で据え置く (手動の固定用)
    auto … _weekend_gate_hold で判定する (weekend_prefetch が渡す。判定はこの1か所に置く)。
           基準表を作る**この時点**の時刻で決める — 生成の step は最長40分ほどかかるので、
           生成を始めた時刻で決めると、8:5x に始まった回が 9:00 をまたいで引き直してしまう
    それ以外 (未設定) … 引き直す (weekly_retrain など)
    """
    v = (os.environ.get("KEEP_BET_GATE") or "").strip().lower()
    if v == "1":
        return True, "KEEP_BET_GATE=1"
    if v == "auto":
        hold, why = _weekend_gate_hold(now)
        if not hold:
            print(f"🎚 閾値 (投資ゲート・信頼度): KEEP_BET_GATE=auto → {why} → 引き直す")
            return False, None
        return True, f"週末の途中 ({why})"
    return False, None


def _bet_gate_or_keep(conn, keep=False, why=None):
    """週末の途中 (KEEP_BET_GATE) なら既存の bet_gate をそのまま使う (#161 / #173)。

    weekend_prefetch は金曜夜だけでなく**土曜の夕方〜夜・日曜未明**にも走り、基準表を作り直す
    (#173 で土曜の回を足した)。そこで投資ゲートの閾値が変わると、同じ週末の土曜と日曜で
    見送りの基準が変わる (#161 の修正後は 0/24 → 約3割見送り)。週末の途中では投資判断を動かさない。
    """
    if keep:
        try:
            with open(OUT, encoding="utf-8") as f:
                prev = json.load(f).get("bet_gate")
            if prev:
                print(f"🎚 投資ゲート: {why} → 既存の閾値を維持 {prev.get('thresholds')}")
                # #170: 閾値は動かさず、現モデルだけの窓との遮断率の差だけ記録する。
                # 失敗しても据え置き (本来の仕事) は止めない
                prev = dict(prev)
                prev["keep_reason"] = why   # #173: どの回がなぜ据え置いたか (毎回上書き)
                try:
                    prev["sunday_check"] = _sunday_gate_check(conn, prev)
                except Exception as e:
                    prev["sunday_check"] = {"available": False, "error": f"{type(e).__name__}: {e}"}
                    print(f"   ⚠️ 据え置き閾値の点検に失敗: {type(e).__name__}: {e}")
                return prev
        except (OSError, ValueError):
            pass
        print(f"🎚 投資ゲート: {why} だが既存値が読めない → 作り直す")
    return build_bet_gate_thresholds(conn)


# 据え置いた信頼度の dict を main() がそのまま表示するのに要るキー
_CONFIDENCE_KEYS = ("thresholds", "sample", "n", "target_mix", "mix_before_adjust",
                    "mix_after_adjust", "adjusted_share", "realized_mix", "realized_n",
                    "realized_new_logic_since", "realized_new_logic_mix", "realized_new_logic_n")


def _confidence_or_keep(conn, keep=False, why=None):
    """週末の途中 (KEEP_BET_GATE) なら既存の信頼度の閾値をそのまま使う (#173)。

    信頼度は should_bet の C/D 遮断 (#25) と投稿レースの選定 (S/A/B) に効くので、投資ゲートと
    同じく見送りの基準の一部。#161 は投資ゲートだけを据え置いていたため、週末の途中の回でも
    信頼度の閾値は動いていた (11/07 08:21 の回 S≥35.4 / A≥24.9 / B≥13.9 / C≥9.3 →
    11/08 07:08 の回 S≥35.4 / A≥24.5 / B≥13.6 / C≥9.4)。#173 で土曜の回を足すと、
    動く機会が土曜に最大3回増えるので、投資ゲートと同じ判定で据え置く。
    据え置いた時も引き直した値は if_rebuilt に残す (次の金曜にどれだけ動くかの目安)。
    """
    fresh = build_confidence_thresholds(conn)
    if not keep:
        return fresh
    try:
        with open(OUT, encoding="utf-8") as f:
            prev = json.load(f).get("confidence")
    except (OSError, ValueError):
        prev = None
    if isinstance(prev, dict) and prev.get("available") and all(k in prev for k in _CONFIDENCE_KEYS):
        held = dict(prev)
        held["keep_reason"] = why
        held["if_rebuilt"] = ({"thresholds": fresh["thresholds"], "n": fresh["n"],
                               "sample_period": fresh.get("sample_period")}
                              if fresh.get("available") else
                              {"available": False, "note": fresh.get("note")})
        print(f"🎚 信頼度: {why} → 既存の閾値を維持 {prev['thresholds']} "
              f"(引き直した場合: {held['if_rebuilt'].get('thresholds')})")
        return held
    print(f"🎚 信頼度: {why} だが既存値が読めない → 作り直す")
    return fresh


def _sunday_gate_check(conn, kept):
    """週末の途中 (KEEP_BET_GATE) に据え置いた閾値が、現モデルの分布とずれていないかを測る (#170)。

    毎週再学習 (#169) では、金曜〜土曜未明に引く閾値は毎週「土曜分の新モデル + 前モデル」の
    拡張窓から来る (build_bet_gate_thresholds)。現モデルだけで30件を超える最初の時点は日曜未明
    (3場開催の時だけ) だが、そこで引き直すと土曜と日曜で見送りの基準が変わる (#161) ので閾値は
    動かさない。代わりに、現モデルだけの窓で引いた閾値と据え置いた閾値のそれぞれで、同じ窓の
    遮断率を並べて残す (window_matches_model が毎週 false で情報を持たない分の補い)。
    事後の検知は check_raceday_completeness (c) が別にやる。
    #173: 土曜の最後の作り直し (9:00) 以降 (夕方〜夜の回) にも走る。JSON のキー名 sunday_check は
    互換のため変えない。
    """
    since, since_utc = current_model_deployed_at(conn)
    if not since_utc:
        print("   🔎 据え置き閾値の点検: モデルの配備時刻が取れない → 比較しない")
        return {"available": False, "note": "モデルの配備時刻が取れない"}
    rows = list(conn.execute(f"""
        SELECT pc.predictions_json, g.race_name FROM predictions_cache pc
        JOIN races g ON pc.race_id = g.race_id
        WHERE pc.created_at >= ? AND {PRE_START_SQL}
        ORDER BY g.race_date DESC LIMIT 400
    """, (since_utc,)))
    tops, sums = _gate_samples(rows)
    out = {"available": False, "n": len(tops), "model_deployed_at": since}
    if len(tops) < MIN_SAMPLE_FOR_THRESHOLDS:
        out["note"] = (f"現モデルだけの窓の使えるレースが {len(tops)} 件 "
                       f"(< {MIN_SAMPLE_FOR_THRESHOLDS}) → 比較しない")
        print(f"   🔎 据え置き閾値の点検: {out['note']}")
        return out
    kept_th = (kept or {}).get("thresholds") or {}
    if not ("top_prob" in kept_th and "top3_sum" in kept_th):
        out["note"] = "据え置いた閾値が読めない → 比較しない"
        print(f"   🔎 据え置き閾値の点検: {out['note']}")
        return out
    kept_th = {"top_prob": float(kept_th["top_prob"]), "top3_sum": float(kept_th["top3_sum"])}
    cur_th = _quantile_thresholds(tops, sums)
    bk = _block_rates(tops, sums, kept_th)
    bc = _block_rates(tops, sums, cur_th)
    diff = {k: round(100 * (bk[k] - bc[k]), 1) for k in ("top_prob", "top3_sum", "either")}
    out.update({"available": True, "thresholds_kept": kept_th, "thresholds_current_window": cur_th,
                "block_rate_kept": bk, "block_rate_current_window": bc, "diff_pt": diff,
                "note": "閾値は据え置き (KEEP_BET_GATE)。比較のみ"})
    _p = lambda r: f"{100 * r['top_prob']:.0f}%/{100 * r['top3_sum']:.0f}% (どちらか {100 * r['either']:.0f}%)"
    print(f"   🔎 据え置き閾値の点検 (現モデルだけの窓 n={len(tops)}、閾値は動かさない): "
          f"据え置き {kept_th['top_prob']}/{kept_th['top3_sum']} → 遮断 {_p(bk)} | "
          f"この窓で引くと {cur_th['top_prob']}/{cur_th['top3_sum']} → 遮断 {_p(bc)} | "
          f"差 {diff['top_prob']:+.1f}/{diff['top3_sum']:+.1f}pt")
    if max(abs(diff["top_prob"]), abs(diff["top3_sum"])) >= 10:
        print("   ⚠️ 据え置いた閾値の遮断率が、現モデルだけの窓で引いた場合と 10pt 以上ずれている "
              "(来週の金曜に引き直される。投資ゲートの遮断率は check_raceday_completeness (c) も見る)")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=os.path.join(ROOT, "keiba.db"))
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    # #173: 週末の途中か (見送りの基準になる閾値を据え置くか) は1回だけ判定し、両方に渡す
    keep, why = _keep_thresholds_requested()
    data = {
        "generated_at": datetime.now(JST).isoformat(),
        "capture": build_capture(conn),
        "payout": build_payout(conn),
        "confidence": _confidence_or_keep(conn, keep, why),   # #151 / #173
        "bet_gate": _bet_gate_or_keep(conn, keep, why),       # #156 / #161 / #173
    }
    # #169 review: 撤去した過信帯の実額監視。失敗しても基準表 (閾値) の作成は止めない
    try:
        data["confidence_adjust_watch"] = build_adjust_watch(conn)
    except Exception as e:
        data["confidence_adjust_watch"] = {"available": False, "error": f"{type(e).__name__}: {e}"}

    print("📊 レース条件別の実測値")
    for g, gl in (("flat", "平場"), ("maiden", "未勝利・新馬・障害")):
        print(f"  【{gl}】")
        for cb in data["capture"]["groups"][g]["buckets"]:
            pb = next((x for x in data["payout"]["groups"][g]["buckets"]
                       if x["label"] == cb["label"]), {})
            if "cap5" not in cb:
                print(f"    {cb['label']:8s} 母数 {cb['n']:4d} — 少なすぎるため非表示")
                continue
            print(f"    {cb['label']:8s} n={cb['n']:4d}  印5頭 {100*cb['cap5']:5.1f}%  印7頭 {100*cb['cap7']:5.1f}%"
                  f"  三連複中央値 {pb.get('trio_median', 0):7,}円  1万円超 {100*pb.get('trio_over10k', 0):4.1f}%")
    cf = data["confidence"]
    if cf.get("available"):
        print(f"\n🎚 信頼度の閾値 ({cf['sample']}, n={cf['n']}): "
              + " / ".join(f"{k}≥{v}" for k, v in cf["thresholds"].items()))
        # #169: 補正前・補正後 (=目標を当てる側)・実際に保存された構成比を並べて出す
        _fmt = lambda m: "/".join(f"{g}{100 * m.get(g, 0):.0f}" for g in "SABCD")
        print(f"   構成比 目標 {_fmt(cf['target_mix'])} | 補正前 {_fmt(cf['mix_before_adjust'])}"
              f" → 補正後 {_fmt(cf['mix_after_adjust'])} (補正が掛かった割合 {100 * cf['adjusted_share']:.0f}%)")
        print(f"   直近に実際に保存された構成比 {_fmt(cf['realized_mix'])} (n={cf['realized_n']}, "
              f"各予測の時点の閾値・補正で付いたもの)")
        print(f"   うち新しい補正 ({cf['realized_new_logic_since']} 以降) で付いた分 "
              + (f"{_fmt(cf['realized_new_logic_mix'])} (n={cf['realized_new_logic_n']})"
                 if cf['realized_new_logic_n'] else "まだ無い (n=0)"))
    else:
        print(f"\n⚠️ 信頼度の閾値: {cf.get('note')} → 従来の固定値を使う")

    w = data["confidence_adjust_watch"]
    if "band" in w:
        _r = lambda x: f"{100 * x:.1f}%" if x is not None else "-"
        d = w.get("diff")
        print(f"\n👀 撤去した過信帯 (◎{w['band_odds'][0]}倍以上{w['band_odds'][1]}倍未満) の実額 ({w['since']} 以降): "
              f"帯 n={w['band']['n']} 回収 {_r(w['band']['roi'])} / 残り n={w['rest']['n']} 回収 {_r(w['rest']['roi'])}"
              + (f" / 差 {100 * d['est']:+.1f}pt [{100 * d['ci95'][0]:+.1f}, {100 * d['ci95'][1]:+.1f}]" if d else ""))
        print(f"   → {w['status']}")
    else:
        print(f"\n⚠️ 過信帯の実額監視に失敗: {w.get('error')}")

    if args.dry_run:
        print("\n(--dry-run: 書き込みなし)")
        return 0
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"\n✅ {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
