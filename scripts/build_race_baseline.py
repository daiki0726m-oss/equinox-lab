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
    import subprocess, urllib.request
    rel = "models/model_rank.pkl"
    utc = None
    repo = os.environ.get("GITHUB_REPOSITORY", "daiki0726m-oss/equinox-lab")
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/commits?path={rel}&per_page=1",
            headers={"Accept": "application/vnd.github+json",
                     **({"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"}
                        if os.environ.get("GITHUB_TOKEN") else {})})
        with urllib.request.urlopen(req, timeout=20) as r:
            ts = json.load(r)[0]["commit"]["committer"]["date"]
        utc = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        try:
            shallow = subprocess.run(["git", "rev-parse", "--is-shallow-repository"], cwd=ROOT,
                                     capture_output=True, text=True, timeout=30).stdout.strip()
            if shallow == "false":
                out = subprocess.run(["git", "log", "-1", "--format=%cI", "--", rel],
                                     cwd=ROOT, capture_output=True, text=True, timeout=30)
                if out.returncode == 0 and out.stdout.strip():
                    utc = datetime.fromisoformat(out.stdout.strip())
        except Exception:
            pass
    if utc is None:
        return None, None     # 不明 → 呼び出し側は全期間を使い「窓が合っていない」と明示する
    jst = utc.astimezone(timezone(timedelta(hours=9)))
    return (jst.strftime("%Y-%m-%d %H:%M:%S"),
            utc.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))


def model_uncommitted(rel="models/model_rank.pkl"):
    """作業ツリーの models/model_rank.pkl が HEAD と違う (= 再学習したが未 commit) か。

    #169: weekly_retrain は「再学習 → 基準表 → commit」の順に動く。基準表を作る時点では
    新モデルが origin に無いので、current_model_deployed_at() は**前のモデル**の時刻を返し、
    前モデルの予測が30件以上あると window_matches_model=true と誤記録していた
    (11/02・10/19・9/28 の3回。誤っていたのは記録だけで、閾値の値はフラグが正しくても同じ)。
    git status は index に記録されたハッシュと作業ファイルを比べるだけなので、
    shallow / 部分クローン (blob:none) でも過去の blob を取りに行かない。
    判定できない時 (git が無い等) は False (従来どおり)。
    """
    import subprocess
    try:
        out = subprocess.run(["git", "status", "--porcelain", "--", rel],
                             cwd=ROOT, capture_output=True, text=True, timeout=30)
        return out.returncode == 0 and bool(out.stdout.strip())
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
    """
    # 2026-05 の実測遮断率 = 閾値が意図した水準
    TARGET = {"top_prob": 0.30, "top3_sum": 0.25}
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
            SELECT pc.predictions_json, g.race_name FROM predictions_cache pc
            JOIN races g ON pc.race_id = g.race_id
            WHERE pc.created_at >= ? AND {PRE_START_SQL}
            ORDER BY g.race_date DESC LIMIT 400
        """, (since_utc,)))   # created_at は UTC (SQLite CURRENT_TIMESTAMP)
    tops, sums = _gate_samples(rows)
    # #161: 標本数は **使えるレース数** (障害・未勝利・新馬を除いた後) で判定する。
    # 旧版は除外前の行数で判定したため、30〜55行の窓 (= 再学習直後の週末1日分) では
    # 除外後に30を割って固定値 0.12/0.30 に落ちていた。足りなければ全期間に広げ、
    # 「前モデルの分布を含む」と明示する (window_matches_model=False)。
    # 広げる範囲は **直近120レース** (使えるのは約60)。旧版の 400 は 2〜3世代前のモデル
    # (8月は sd 0.115 と平坦) まで混ぜるので、再学習直後の閾値が 0.1001/0.2613 =
    # ほぼ素通りに戻っていた (#161 review)。直前のモデルが新モデルに最も近い手掛かり。
    widened = False
    if len(tops) < MIN_SAMPLE_FOR_THRESHOLDS:
        fresh = False
        widened = True
        rows = list(conn.execute(f"""
            SELECT pc.predictions_json, g.race_name FROM predictions_cache pc
            JOIN races g ON pc.race_id = g.race_id
            WHERE {PRE_START_SQL}
            ORDER BY g.race_date DESC, pc.created_at DESC LIMIT 120
        """))
        tops, sums = _gate_samples(rows)

    if len(tops) < MIN_SAMPLE_FOR_THRESHOLDS:
        print(f"🎚 投資ゲート: 標本 {len(tops)} 件は不足 → 固定値 {FALLBACK} を維持")
        return {"available": False, "n": len(tops), "thresholds": FALLBACK}

    tops.sort(); sums.sort()
    if retrained:
        print(f"   ⚠️ 再学習した新モデルが未 commit。窓の起点 {since or '不明'} は前のモデルの配備時刻で、"
              f"閾値は前モデルの予測分布から引いています (window_matches_model=False)")
    elif widened:
        print(f"   ⚠️ 現モデル ({since or '配備時刻不明'}) 以降の使えるレースが "
              f"{MIN_SAMPLE_FOR_THRESHOLDS} 件未満。直前のモデルを含む直近120レースから引いています "
              f"(window_matches_model=False)")
    th = {
        "top_prob": round(tops[int(len(tops) * TARGET["top_prob"])], 4),
        "top3_sum": round(sums[int(len(sums) * TARGET["top3_sum"])], 4),
    }
    print(f"🎚 投資ゲートの閾値 (n={len(tops)}): "
          f"top_prob≥{th['top_prob']:.3f} / top3_sum≥{th['top3_sum']:.3f} "
          f"(目標遮断率 {TARGET['top_prob']:.0%}/{TARGET['top3_sum']:.0%})")
    return {"available": True, "n": len(tops), "target_block_rate": TARGET,
            "model_deployed_at": since, "window_matches_model": fresh,
            "model_retrained_uncommitted": retrained,   # #169
            "thresholds": th}


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
        samples, realized, dates = [], [], []
        for r in conn.execute(f"""
            SELECT pc.predictions_json, pc.confidence, g.race_name, g.race_date
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
            # 実際に保存された信頼度 (旧閾値・旧補正で付いたもの)。目標から崩れていないかの監視用
            "realized_mix": _mix(realized), "realized_n": len(realized)}


def _bet_gate_or_keep(conn):
    """KEEP_BET_GATE=1 なら既存の bet_gate をそのまま使う (#161)。

    weekend_prefetch は金曜夜だけでなく**土曜夜 (日曜 03:30)** にも走り、基準表を作り直す。
    そこで投資ゲートの閾値が変わると、同じ週末の土曜と日曜で見送りの基準が変わる
    (#161 の修正後は 0/24 → 約3割見送り)。週末の途中では投資判断を動かさない。
    """
    if os.environ.get("KEEP_BET_GATE") == "1":
        try:
            with open(OUT, encoding="utf-8") as f:
                prev = json.load(f).get("bet_gate")
            if prev:
                print(f"🎚 投資ゲート: KEEP_BET_GATE=1 → 既存の閾値を維持 {prev.get('thresholds')}")
                return prev
        except (OSError, ValueError):
            pass
        print("🎚 投資ゲート: KEEP_BET_GATE=1 だが既存値が読めない → 作り直す")
    return build_bet_gate_thresholds(conn)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=os.path.join(ROOT, "keiba.db"))
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    data = {
        "generated_at": datetime.now(JST).isoformat(),
        "capture": build_capture(conn),
        "payout": build_payout(conn),
        "confidence": build_confidence_thresholds(conn),
        "bet_gate": _bet_gate_or_keep(conn),   # #156 / #161
    }

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
        print(f"   直近に実際に保存された構成比 {_fmt(cf['realized_mix'])} (n={cf['realized_n']}, 旧閾値・旧補正)")
    else:
        print(f"\n⚠️ 信頼度の閾値: {cf.get('note')} → 従来の固定値を使う")

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
