#!/usr/bin/env python3
"""開催日の「やり残し」を翌朝に検査する (#161)。

2026-09-21(月・祝) は、予想を投稿したのに
  - 阪神 10R/11R(神戸新聞杯G2)/12R の着順が収集されず
  - 結果報告が一度も配信されなかった
が、どちらも通知されず、監査で見つかるまで5日間誰も知らなかった。
各層 (runner / watchdog / セーフティネット) は「自分の担当時刻に何をしたか」しか見ておらず、
「その日が最後まで終わったか」を見る者がいなかった。

ここでは直近の開催日 (race_calendar.json、今日より前) ごとに:
  1. 出走馬がいるのに1着が記録されていないレース (= 着順の取りこぼし)
  2. 予想を投稿した (posted_marks がある) のに results の lock が無い (= 結果報告の未配信)
を調べ、見つかれば exit 1 (feature_parity.yml が Issue を立てる)。

着順の取りこぼしは collect_results の夜間スイープ (#161) が直近10日分を自動で拾い直すので、
ここに出るのは「スイープでも拾えなかった」もの。結果報告の遅れ配信は人が判断する。

#169 (2026-11-05): 予測そのものの点検を足した (週末明けの自動点検)
-------------------------------------------------------------------
10/31・11/01 の反省で、次の3つがどれも監視の外で起きていたと分かった。
  (a) 9時台にオッズを取り直した再予測が、race_day_runner の push で毎回古い版に巻き戻され、
      投稿された印・買い目は runner 起動前 (07:56/08:00) の予測だった (#68 以降・毎開催日)
  (b) 11/01 未明、仮馬番 (五十音順) の予想 JSON が 6時間18分公開されていた
  (c) 投資ゲートの遮断率が、再学習のたびに目標 (30%/25%) から外れる (#156 の再発監視)
オーケストレーター自身の反省「確かめると約束しながら仕組みを作らなかった」の再発防止として、
人の記憶でなく、この毎朝の検査で見る。材料は公開した予測 JSON の git 履歴
(scripts/pred_history.py)。DB は並行 workflow に上書きされるので使わない。
CI の checkout は shallow なので、feature_parity.yml が履歴だけを持つ部分クローン
(/tmp/hist、blob は docs/data の予測 JSON だけを後から取る) を別に作り --history-repo で渡す。
天候・馬場の充足率は W 班が別に足すので、ここでは見ない。
"""
import argparse
import datetime as dt
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "docs", "data")
JST = dt.timezone(dt.timedelta(hours=9))


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ───────────────────────── #169 予測の点検 ─────────────────────────

def _fmt(t):
    return t.strftime("%m/%d %H:%M") if t else "-"


def _dur(sec):
    m = int(round(sec / 60))
    return f"{m // 60}時間{m % 60}分" if m >= 60 else f"{m}分"


def _ref_numbers(conn, d, latest_doc):
    """確定版の 馬番→馬名。DB の results (取消も含む) を正とし、無いレースは最新の JSON で代用。"""
    import pred_history as ph
    ref = {}
    if conn is not None:
        for rid, hn, nm in conn.execute("""
            SELECT r.race_id, r.horse_number, COALESCE(h.horse_name, '')
            FROM results r JOIN races g ON g.race_id = r.race_id
            LEFT JOIN horses h ON h.horse_id = r.horse_id
            WHERE g.race_date = ?""", (d,)):
            if nm:
                ref.setdefault(str(rid), {})[int(hn)] = nm.strip()
    for rid, r in ph.races_of(latest_doc).items():
        if rid not in ref:
            ref[rid] = {int(h["horse_number"]): (h.get("horse_name") or "").strip()
                        for h in r.get("horses") or [] if h.get("horse_number") is not None}
    return ref


def _number_mismatch(doc, ref):
    """その版で、確定版と馬番↔馬名がずれている (または確定版にいない) 馬。{race_id: 頭数}"""
    import pred_history as ph
    bad = {}
    for rid, r in ph.races_of(doc).items():
        rr = ref.get(rid)
        if not rr:
            continue
        n = sum(1 for h in r.get("horses") or []
                if rr.get(int(h.get("horse_number") or 0)) != (h.get("horse_name") or "").strip())
        if n:
            bad[rid] = n
    return bad


def check_predictions(d, hist, conn, now):
    """1開催日について (a) 巻き戻し・投稿印の一致 (b) 仮馬番の公開 (c) 投資ゲートの遮断率 を見る。

    返り値: (lines, gaps, gate) — lines は報告用の行、gaps は異常として通知する行。
    """
    import pred_history as ph
    from race_utils import is_ml_out_of_domain
    d8 = d.replace("-", "")
    day0 = dt.datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=JST)
    end = min(day0 + dt.timedelta(days=1, hours=6), now)   # 翌朝6時まで (結果反映の版を含む)
    lines, gaps = [], []
    pm = ph.load_posted_marks(d8, DATA) or {}
    posted = pm.get("races") or {}
    post_at = ph.parse_jst(pm.get("posted_at"))
    vs = hist.versions(d8, until=end) if hist is not None else []
    if vs and os.environ.get("PRED_HISTORY_PREFETCH") == "1":
        hist.prefetch(d8, vs)
    docs = [(v, hist.load(d8, v["sha"])) for v in vs]
    docs = [(v, doc) for v, doc in docs if doc]
    pre_doc, pre_src = None, None
    if not docs:
        if hist is not None:
            # 履歴は読めるのにその日の版が1つも無い = 予測 JSON が一度も公開されていない
            gaps.append(f"{d}: 予測 JSON の公開版が履歴に1つも無い (ダッシュボードが404だった疑い)")
        else:
            lines.append(f"{d}: 予測 JSON の履歴が読めない — (a)(b) は判定不能")
        pre_doc = _load(os.path.join(DATA, f"predictions_{d8}.json"))
        pre_src = "現在の JSON (履歴なし)"
    else:
        lines.append(f"{d}: 予測 JSON の公開版 {len(docs)}個 ({_fmt(docs[0][0]['at'])}〜{_fmt(docs[-1][0]['at'])})")

    # ── (a) 巻き戻し: 新しい予測が出た後に、それより古い予測内容へ戻った版 ──
    # 予測の中身 (LGB 出力と印) の指紋を初出順に「世代」として番号を振り、
    # 一度見た最新の世代より古い世代が後から出たら巻き戻しとみなす。
    # オッズ・着順は予測後に正当に変わるので指紋に入れない (pred_history.pred_fingerprint)。
    gens, maxg, events, state_at, first_at = {}, {}, [], [], {}
    for v, doc in docs:
        st = {}
        for rid, r in ph.races_of(doc).items():
            fp = ph.pred_fingerprint(r)
            g = gens.setdefault(rid, [])
            if fp not in g:
                g.append(fp)
                first_at.setdefault(rid, []).append(v["at"])
            gi = g.index(fp)
            st[rid] = gi
            if gi < maxg.get(rid, -1):
                events.append((v, rid))
            maxg[rid] = max(maxg.get(rid, -1), gi)
        state_at.append((v, doc, st))
    if docs and post_at is not None:
        # 投稿後に予測内容が作り直された (10時ガードの素通り等): 投稿した印とダッシュボードが食い違う
        late = sorted({rid for rid, ts in first_at.items() for t in ts[1:] if t >= post_at})
        if late:
            t0 = min(t for rid in late for t in first_at[rid][1:] if t >= post_at)
            gaps.append(f"{d}: 投稿 ({_fmt(post_at)}) の後に予測内容が作り直された {len(late)}R (最初 {_fmt(t0)})")
    if docs:
        if events:
            rbv = sorted({e[0]["sha"]: e[0] for e in events}.values(), key=lambda x: x["at"])
            races = {e[1] for e in events}
            n_pre = len({e[0]["sha"] for e in events if post_at is None or e[0]["at"] < post_at})
            gaps.append(f"{d}: 予測の巻き戻し {len(rbv)}版・{len(races)}R — 新しい予測が出た後に古い予測内容へ"
                        f"戻った (最初 {_fmt(rbv[0]['at'])}「{rbv[0]['subject'][:40]}」、うち投稿前 {n_pre}版)")
        else:
            lines.append(f"{d}: (a) 予測の巻き戻し なし")

    # ── 投稿した印 vs 投稿直前の公開版 / 投稿前に作られた最新の予測 ──
    if docs and posted:
        before = [(v, doc, st) for v, doc, st in state_at if post_at is None or v["at"] < post_at]
        if before:
            v_pre, pre_doc, _st = before[-1]
            pre_src = f"投稿直前の公開版 {_fmt(v_pre['at'])}"
            pre_races = ph.races_of(pre_doc)
            m_pre = m_new = n = 0
            newest_at = set()
            for rid, ents in posted.items():
                want = ph.posted_mark_set(ents)
                n += 1
                r = pre_races.get(rid)
                m_pre += int(bool(r) and ph.marks_of(r) == want)
                gmax = max((st.get(rid, -1) for _v, _d, st in before), default=-1)
                newest = next((ph.races_of(dd).get(rid) for _v, dd, st in reversed(before)
                               if st.get(rid) == gmax), None)
                m_new += int(bool(newest) and ph.marks_of(newest) == want)
                if gmax >= 0 and first_at.get(rid):
                    newest_at.add(_fmt(first_at[rid][gmax]))
            msg = (f"{d}: 投稿印 {n}R — 投稿直前の公開版と一致 {m_pre}/{n}、"
                   f"投稿前に作られた最新の予測 ({'・'.join(sorted(newest_at))} 初出) と一致 {m_new}/{n}")
            if m_pre < n or m_new < n:
                gaps.append(msg + (f" — {n - m_new}R は最新の予測でない版の印で投稿" if m_new < n else ""))
            else:
                lines.append(msg)

    # ── (b) 仮馬番: 確定版と馬番↔馬名がずれた JSON が公開されていた時間 ──
    if docs:
        ref = _ref_numbers(conn, d, docs[-1][1])
        spans, worst = [], (0, 0)
        for i, (v, doc) in enumerate(docs):
            bad = _number_mismatch(doc, ref)
            if bad:
                t1 = docs[i + 1][0]["at"] if i + 1 < len(docs) else end
                spans.append([v["at"], t1])
                worst = max(worst, (len(bad), sum(bad.values())))
        if spans:
            merged = []
            for a, b in spans:          # 続いた版はひとつの期間にまとめる
                if merged and a <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], b)
                else:
                    merged.append([a, b])
            tot = sum((b - a).total_seconds() for a, b in merged)
            gaps.append(f"{d}: 確定版と馬番がずれた予測 JSON が公開されていた時間 {_dur(tot)} ("
                        + "、".join(f"{_fmt(a)}〜{_fmt(b)}" for a, b in merged)
                        + f"、最大 {worst[0]}R・{worst[1]}頭)")
        else:
            lines.append(f"{d}: (b) 馬番のずれた公開版 なし")

    # ── (c) 投資ゲートの遮断率 (投稿直前の公開版 = 判断に使った予測) ──
    cut = post_at or (day0 + dt.timedelta(hours=12))
    if pre_doc is None and docs:
        cand = [(v, doc) for v, doc in docs if v["at"] < cut]
        v0, pre_doc = cand[-1] if cand else docs[-1]
        pre_src = f"{_fmt(v0['at'])} の公開版"
    gate = None
    if pre_doc:
        bl = hist.file_at("docs/data/race_baseline.json", cut) if hist is not None else None
        bl_src = "当日朝の race_baseline.json" if bl else "現在の race_baseline.json"
        bl = bl or _load(os.path.join(DATA, "race_baseline.json")) or {}
        th = ((bl.get("bet_gate") or {}).get("thresholds")) or {"top_prob": 0.12, "top3_sum": 0.30}
        rates = ph.gate_block_rates(ph.races_of(pre_doc).values(), th, is_ml_out_of_domain)
        gate = {"date": d, "src": pre_src, "baseline": bl_src, "thresholds": th, **rates}
        if rates.get("n"):
            lines.append(f"{d}: (c) 投資ゲート遮断率 平場{rates['n']}R ({pre_src}、閾値は{bl_src} "
                         f"top_prob≥{th['top_prob']} / top3_sum≥{th['top3_sum']}): "
                         f"top_prob {rates['top_prob']:.0%} (目標30%) / top3_sum {rates['top3_sum']:.0%} (目標25%)")
    return lines, gaps, gate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default=os.environ.get("COMPLETENESS_REPORT", "/tmp/raceday_gaps.md"))
    # #169: 予測の点検 (巻き戻し・仮馬番・投資ゲート)
    ap.add_argument("--quality-out", default=os.environ.get("QUALITY_REPORT", "/tmp/raceday_quality.md"))
    ap.add_argument("--quality-days", type=int, default=2,
                    help="予測の点検をする開催日 (今日から N 日前まで。翌朝と翌々朝に見る)")
    ap.add_argument("--history-repo", default=os.environ.get("RACEDAY_HISTORY_REPO"),
                    help="予測 JSON の git 履歴を持つ clone (既定: このリポジトリ)")
    ap.add_argument("--history-cache", default=os.environ.get("PRED_HISTORY_CACHE"),
                    help="読んだ版を置く場所 (任意)")
    ap.add_argument("--dates", nargs="*", help="開催日 YYYY-MM-DD を直接指定 (過去の週末の再点検用)")
    ap.add_argument("--now", default=None, help="基準時刻 JST 'YYYY-MM-DD HH:MM' (再現用)")
    args = ap.parse_args()

    now = (dt.datetime.strptime(args.now, "%Y-%m-%d %H:%M").replace(tzinfo=JST) if args.now
           else dt.datetime.now(JST))
    today = now.date()
    cal = _load(os.path.join(DATA, "race_calendar.json")) or {}
    if args.dates:
        days = sorted(args.dates)
    else:
        days = sorted(d for d in (cal.get("dates") or {})
                      if (today - dt.timedelta(days=args.days)).isoformat() <= d < today.isoformat())
    if not days:
        print("ℹ️ 検査対象の開催日なし")
        return 0

    db = os.path.join(ROOT, "keiba.db")
    conn = sqlite3.connect(db) if os.path.exists(db) else None
    gaps = []
    for d in days:
        d8 = d.replace("-", "")
        if conn is not None:
            rows = conn.execute("""
                SELECT g.venue, g.race_number, g.race_name FROM races g
                WHERE g.race_date = ?
                  AND CAST(substr(g.race_id, 5, 2) AS INT) BETWEEN 1 AND 10
                  AND EXISTS (SELECT 1 FROM results r WHERE r.race_id = g.race_id)
                  AND NOT EXISTS (SELECT 1 FROM results w
                                  WHERE w.race_id = g.race_id AND w.finish_position = 1)
                ORDER BY g.venue, g.race_number
            """, (d,)).fetchall()
            if rows:
                gaps.append(f"{d}: 着順が未収集 {len(rows)}R — "
                            + "、".join(f"{v}{n}R {nm}" for v, n, nm in rows))
        marks = _load(os.path.join(DATA, f"posted_marks_{d8}.json"))
        slots = (_load(os.path.join(DATA, f"posted_slots_{d8}.json")) or {}).get("slots") or {}
        if marks and "results" not in slots:
            gaps.append(f"{d}: 予想は投稿済み (posted_marks あり) だが結果報告が未配信 "
                        f"(posted_slots に results が無い)")

    print(f"🔎 開催日の完了検査: {', '.join(days)}")
    if not gaps:
        print("✅ やり残しなし")
    for g in gaps:
        print(f"❌ {g}")
    try:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("".join(f"- {g}\n" for g in gaps))
    except OSError:
        pass

    # ── #169: 予測の点検 (直近 quality_days 日の開催日。翌朝・翌々朝に見る) ──
    qgaps = []
    try:
        for p in (ROOT, os.path.join(ROOT, "scripts")):
            if p not in sys.path:
                sys.path.insert(0, p)
        import pred_history as ph
        qdays = days if args.dates else [
            d for d in days if (today - dt.timedelta(days=args.quality_days)).isoformat() <= d]
        hist = ph.History(args.history_repo or ROOT, cache_dir=args.history_cache)
        if not hist.available():
            # 点検ができなかったことを黙らせない (「確かめる」と言って確かめていない状態の再発防止)
            msg = (f"予測 JSON の履歴が読めず ({args.history_repo or ROOT})、巻き戻し・仮馬番の点検が"
                   f"できなかった — feature_parity.yml の「予測 JSON の履歴を取得」の step を確認")
            print(f"⚠️ {msg}")
            if qdays:
                qgaps.append(msg)
            hist = None
        gates = []
        if qdays:
            print(f"\n🔎 予測の点検 (#169): {', '.join(qdays)}")
        for d in qdays:
            lines, gg, gate = check_predictions(d, hist, conn, now)
            for ln in lines:
                print(f"   {ln}")
            for g in gg:
                print(f"❌ {g}")
            qgaps += gg
            if gate and gate.get("n"):
                gates.append(gate)
        if gates:
            msg, off = ph.gate_verdict(gates)
            if msg:
                print(("❌ " if off else "   ") + msg)
                if off:
                    qgaps.append(msg)
        if qdays and not qgaps:
            print("✅ 予測の点検で異常なし")
    except Exception as e:      # 点検の失敗で、着順・結果報告の完了検査の通知まで止めない
        print(f"⚠️ 予測の点検に失敗: {type(e).__name__}: {e}")
        qgaps.append(f"予測の点検 (#169) が例外で止まった: {type(e).__name__}: {e}")
    try:
        with open(args.quality_out, "w", encoding="utf-8") as f:
            f.write("".join(f"- {g}\n" for g in qgaps))
    except OSError:
        pass
    return 1 if (gaps or qgaps) else 0


if __name__ == "__main__":
    sys.exit(main())
