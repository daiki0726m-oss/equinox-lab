#!/usr/bin/env python3
"""👻 幽霊馬の検出・除去 (#43/#44 の残存データ一掃 + 定期メンテ)

枠順確定前 (月曜) の出馬表は netkeiba の馬番欄が空のため scrape_shutuba が
五十音順で「仮馬番 1..N」を採番する。枠順抽選 (金 11:00) 後の確定馬番 1..M
(M<N) で INSERT OR REPLACE しても、余った仮馬番 M+1..N の行が finish_position=0・
オッズ0 の「幽霊馬」として残存する (CLAUDE.md #43/#44)。save_race_to_db /
predict.py / post_x.py には再発防止を入れたが、過去レースの DB に既に残った
幽霊馬を一掃するのが本スクリプト。

🚨 安全設計 (最重要)
  `finish_position=0` は幽霊馬の指標として **信用できない**。schema 定義どおり
  0 は「除外/取消/競走中止(障害戦のDNF等)」の正規値でもある。DB 全体で 2,300+
  件の正規 finish=0 行が存在し、その多くは実馬 (オッズ・人気あり。例: 1番人気
  2.8倍が競走中止)。従って **「finish=0 を一律削除」は厳禁**。以下の保守的な
  2 基準のみで削除する:

  基準(A) 同一レース内 horse_id 重複
    同じ horse_id が複数の horse_number に存在し、うち1つを「本物」と確定できる
    (finish>0 / live odds API の実出走馬番 / 唯一 odds>0 のいずれか) 場合、
    残りの finish=0 重複行を削除。確定済(finish>0)行は絶対保護。
    → ネットワーク不要。過去レースにも安全に適用可。どちらが本物か決められない
      (全行 finish=0・API不可・odds全0) 場合は削除せず「要手動確認」として報告。

  基準(B) live odds API 実出走馬に無い余剰行 (未開催レース限定)
    レースに finish>0 行が **1つも無い** (= 未開催) 場合に限り、netkeiba オッズ
    API (refresh_odds.fetch_odds_from_api) の実出走馬番と DB の馬番を照合し、
    API に無い余剰行を削除。API が健全な時のみ発火:
      ・API >= 5 頭 (取得失敗・部分取得を弾く)
      ・API ⊆ DB (API にしか無い馬番があれば「出走馬欠落」別バグ → スキップ)
      ・DB 行数 > API 頭数 (余剰が証明できる時のみ。等しい/少ないなら削除しない)
    → 開催済レースには **一切適用しない** (取消・競走中止馬の誤削除を防ぐ)。

デフォルトは dry-run (検出のみ)。実削除は --apply。

CLI:
  python scripts/remove_ghost_horses.py --date 20260531 --apply
  python scripts/remove_ghost_horses.py --months 6           # dry-run, 直近6ヶ月
  python scripts/remove_ghost_horses.py --all --apply        # 全レース実削除
  python scripts/remove_ghost_horses.py --date 20260531 --apply --repredict

返り値: 0=幽霊なし / 1=幽霊検出(dry-run) または削除実行 / 2=エラー
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# repo ルートを import path に追加
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import get_db, init_db  # noqa: E402
from refresh_odds import fetch_odds_from_api  # noqa: E402

JST = timezone(timedelta(hours=9))

# 基準(B) を「最近の未開催レース」に限定し、過去レースへの無駄な API 呼び出しを防ぐ窓
API_RECENT_DAYS = 10
# 基準(B) で API を信頼する最小頭数
API_MIN_HORSES = 5


def now_jst() -> datetime:
    return datetime.now(JST)


def _to_iso(date_str: str) -> str:
    return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"


def fetch_real_horse_numbers(race_id: str) -> set[int] | None:
    """live odds API から実出走馬番の集合を取得。失敗/空なら None。"""
    try:
        odds = fetch_odds_from_api(race_id)
    except Exception as e:  # pragma: no cover - ネットワーク例外
        print(f"    ⚠️ API例外 {race_id}: {e}")
        return None
    if not odds:
        return None
    nums = set()
    for k in odds.keys():
        try:
            nums.add(int(k))
        except (TypeError, ValueError):
            continue
    return nums or None


def _races_in_scope(conn, date_iso: str | None, since_iso: str | None) -> list[dict]:
    """対象レース一覧 (race_id, race_date, venue, race_number, race_name)。"""
    if date_iso:
        date_yyyymmdd = date_iso.replace("-", "")
        rows = conn.execute(
            """SELECT race_id, race_date, venue, race_number, race_name
               FROM races WHERE race_date = ? OR race_date = ?
               ORDER BY race_date, venue, race_number""",
            (date_iso, date_yyyymmdd),
        ).fetchall()
    elif since_iso:
        # race_date は 'YYYY-MM-DD' 文字列前提 (DB の大半)。'YYYYMMDD' 混在に備え両方許容。
        since_yyyymmdd = since_iso.replace("-", "")
        rows = conn.execute(
            """SELECT race_id, race_date, venue, race_number, race_name
               FROM races
               WHERE (length(race_date)=10 AND race_date >= ?)
                  OR (length(race_date)=8  AND race_date >= ?)
               ORDER BY race_date, venue, race_number""",
            (since_iso, since_yyyymmdd),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT race_id, race_date, venue, race_number, race_name
               FROM races ORDER BY race_date, venue, race_number"""
        ).fetchall()
    return [dict(r) for r in rows]


def _race_rows(conn, race_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT re.horse_number, re.horse_id, re.finish_position, re.odds,
                  re.popularity, h.horse_name
           FROM results re LEFT JOIN horses h ON re.horse_id = h.horse_id
           WHERE re.race_id = ? ORDER BY re.horse_number""",
        (race_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _fin(r: dict) -> int:
    return r.get("finish_position") or 0


def _odds(r: dict) -> float:
    return r.get("odds") or 0.0


def detect_criterion_a(rows: list[dict], api_nums: set[int] | None):
    """基準(A): 同一 horse_id の重複から finish=0 の幽霊を特定。

    Returns (ghosts, ambiguous):
      ghosts    : 削除すべき行 dict のリスト (reason 付き)
      ambiguous : 本物を決められず手動確認が要る (race_id, horse_id) の情報
    """
    ghosts: list[dict] = []
    ambiguous: list[dict] = []

    by_hid: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        hid = r.get("horse_id")
        if hid:
            by_hid[hid].append(r)

    for hid, grp in by_hid.items():
        if len(grp) < 2:
            continue  # 重複なし

        finished = [r for r in grp if _fin(r) > 0]
        unfinished = [r for r in grp if _fin(r) == 0]

        if finished:
            # 確定済が本物。finish=0 の重複は幽霊。
            keep = finished[0]
            for r in unfinished:
                ghosts.append({
                    **r,
                    "reason": f"重複馬番(同一horse_id, {keep['horse_number']}番=確定着順{_fin(keep)}着が本物)",
                })
            # 確定済が2つ以上ある異常はデータ矛盾 → 触らず報告のみ
            if len(finished) > 1:
                ambiguous.append({
                    "horse_id": hid, "kind": "multi_finished",
                    "horse_numbers": [r["horse_number"] for r in finished],
                })
            continue

        # 全行 finish=0 (未開催) → 本物を別シグナルで特定
        keep = None
        if api_nums is not None:
            in_api = [r for r in unfinished if r["horse_number"] in api_nums]
            if len(in_api) == 1:
                keep = in_api[0]
        if keep is None:
            with_odds = [r for r in unfinished if _odds(r) > 0]
            if len(with_odds) == 1:
                keep = with_odds[0]

        if keep is not None:
            for r in unfinished:
                if r["horse_number"] != keep["horse_number"]:
                    ghosts.append({
                        **r,
                        "reason": f"重複馬番(同一horse_id, {keep['horse_number']}番=実出走が本物)",
                    })
        else:
            ambiguous.append({
                "horse_id": hid, "kind": "undecidable_duplicate",
                "horse_numbers": [r["horse_number"] for r in unfinished],
            })

    return ghosts, ambiguous


def detect_criterion_b(rows: list[dict], api_nums: set[int] | None,
                       already: set[int]):
    """基準(B): 未開催レースで API 実出走馬に無い余剰行を特定。

    rows は finish>0 が1つも無い (未開催) レースの全行であること。
    already は基準(A)で既に幽霊判定済の horse_number 集合。
    """
    if api_nums is None or len(api_nums) < API_MIN_HORSES:
        return []
    db_nums = {r["horse_number"] for r in rows}
    # API にしか無い馬番がある = DB 側で出走馬欠落 (別バグ)。誤削除回避のためスキップ。
    if not api_nums.issubset(db_nums):
        return []
    # 余剰が証明できる時のみ (DB 行数 > API 実頭数)。
    if len(db_nums) <= len(api_nums):
        return []
    extras = db_nums - api_nums
    ghosts = []
    for r in rows:
        hn = r["horse_number"]
        if hn in extras and hn not in already and _fin(r) == 0:
            ghosts.append({
                **r,
                "reason": f"API実出走({len(api_nums)}頭)に馬番{hn}が不在(余剰幽霊)",
            })
    return ghosts


def remove_ghosts(date: str | None = None, months: int | None = 6,
                  all_races: bool = False, apply: bool = False,
                  use_api: bool = True, verbose: bool = True):
    """幽霊馬を検出 (apply=True で削除)。

    Returns dict: {deleted, detected, affected_dates, ambiguous, races_scanned}
    """
    init_db()

    date_iso = _to_iso(date) if date else None
    since_iso = None
    if not date and not all_races and months:
        since_iso = (now_jst() - timedelta(days=months * 30)).strftime("%Y-%m-%d")

    today = now_jst().date()
    api_window_start = today - timedelta(days=API_RECENT_DAYS)

    findings: list[dict] = []      # 削除対象 (race_id 付き)
    ambiguous_all: list[dict] = []
    affected_dates: set[str] = set()
    races_scanned = 0

    with get_db() as conn:
        races = _races_in_scope(conn, date_iso, since_iso)

        if verbose:
            scope = (f"--date {date}" if date else
                     "--all (全レース)" if all_races else
                     f"直近{months}ヶ月 (>= {since_iso})")
            print(f"🔍 走査範囲: {scope} / {len(races)}レース\n")

        for race in races:
            races_scanned += 1
            rid = race["race_id"]
            rows = _race_rows(conn, rid)
            if not rows:
                continue

            has_finished = any(_fin(r) > 0 for r in rows)

            # race_date を date 比較用に解釈
            rd = race.get("race_date") or ""
            try:
                rdate = datetime.strptime(rd.replace("-", ""), "%Y%m%d").date()
            except ValueError:
                rdate = None

            # API は「未開催 かつ 直近」レースのみ呼ぶ (過去レースは API が空を返すだけ)
            api_nums = None
            if use_api and not has_finished and (rdate is None or rdate >= api_window_start):
                api_nums = fetch_real_horse_numbers(rid)
                time.sleep(0.2)

            # 基準(A) — 全レース対象
            ghosts_a, ambiguous = detect_criterion_a(rows, api_nums)
            already = {g["horse_number"] for g in ghosts_a}

            # 基準(B) — 未開催レース限定
            ghosts_b = []
            if not has_finished:
                ghosts_b = detect_criterion_b(rows, api_nums, already)

            race_ghosts = ghosts_a + ghosts_b
            for amb in ambiguous:
                amb["race_id"] = rid
                amb["race_name"] = race.get("race_name", "")
                ambiguous_all.append(amb)

            if race_ghosts:
                iso = rd if "-" in rd else _to_iso(rd) if len(rd) == 8 else rd
                affected_dates.add(iso)
                for g in race_ghosts:
                    g["race_id"] = rid
                    g["venue"] = race.get("venue", "")
                    g["race_number"] = race.get("race_number", 0)
                    findings.append(g)
                if verbose:
                    label = f"{race.get('venue','')}{race.get('race_number','')}R {race.get('race_name','')}"
                    print(f"👻 {rid} {label}: 幽霊 {len(race_ghosts)}頭")
                    for g in race_ghosts:
                        print(f"     - {g['horse_number']:>2}番 {g.get('horse_name') or '(名称不明)'}"
                              f" odds={_odds(g)} pop={g.get('popularity') or 0} fin={_fin(g)}"
                              f" | {g['reason']}")

        # ── 削除実行 ──
        deleted = 0
        if apply and findings:
            for g in findings:
                # 二重ガード: finish_position=0/NULL のみ削除 (確定着順は絶対保護)
                cur = conn.execute(
                    "DELETE FROM results WHERE race_id = ? AND horse_number = ? "
                    "AND (finish_position = 0 OR finish_position IS NULL)",
                    (g["race_id"], g["horse_number"]),
                )
                deleted += cur.rowcount
            # horse_count を現状に同期 (削除で実頭数が変わったレース)
            for rid in {g["race_id"] for g in findings}:
                n = conn.execute(
                    "SELECT COUNT(*) AS c FROM results WHERE race_id = ?", (rid,)
                ).fetchone()["c"]
                conn.execute("UPDATE races SET horse_count = ? WHERE race_id = ?", (n, rid))

    # ── サマリー ──
    if verbose:
        print()
        if ambiguous_all:
            print(f"⚠️ 要手動確認 (本物を自動判定できない重複) {len(ambiguous_all)}件:")
            for a in ambiguous_all[:20]:
                print(f"     {a['race_id']} {a.get('race_name','')}: horse_id={a['horse_id']}"
                      f" 馬番={a['horse_numbers']} ({a['kind']})")
            print()
        if not findings:
            print("✅ 幽霊馬は検出されませんでした")
        elif apply:
            print(f"🗑 {deleted}行を削除 ({len(affected_dates)}日 / "
                  f"{len({g['race_id'] for g in findings})}レース)")
            print(f"   影響日: {', '.join(sorted(affected_dates))}")
            print("   ⚠️ 該当日の予測は幽霊込みの可能性 → 再予測+再exportを推奨")
        else:
            print(f"🔎 dry-run: 幽霊 {len(findings)}頭を検出 (未削除)。"
                  f"削除するには --apply を付けて再実行")

    return {
        "deleted": deleted if apply else 0,
        "detected": len(findings),
        "affected_dates": sorted(affected_dates),
        "ambiguous": ambiguous_all,
        "races_scanned": races_scanned,
        "findings": findings,
    }


def repredict_and_export(dates: list[str], verbose: bool = True) -> None:
    """幽霊除去後の該当日を再予測 + 再 export。"""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for iso in dates:
        ymd = iso.replace("-", "")
        if verbose:
            print(f"\n🔄 {ymd} を再予測中...")
        try:
            r = subprocess.run(
                ["python3", "predict.py", "predict", "--date", ymd, "--force"],
                cwd=repo, capture_output=True, text=True, timeout=900,
            )
            if r.returncode != 0:
                print(f"  ❌ 再予測失敗: {r.stderr[-300:]}")
                continue
            print("  ✅ 再予測完了")
            e = subprocess.run(
                ["python3", "export_predictions.py", ymd],
                cwd=repo, capture_output=True, text=True, timeout=300,
            )
            if e.returncode == 0:
                print("  ✅ 再export完了")
            else:
                print(f"  ⚠️ export失敗: {e.stderr[-200:]}")
        except Exception as ex:  # pragma: no cover
            print(f"  ❌ 再予測例外: {ex}")


def main():
    ap = argparse.ArgumentParser(description="👻 幽霊馬の検出・除去")
    ap.add_argument("--date", help="対象日 YYYYMMDD (1日のみ)")
    ap.add_argument("--months", type=int, default=6,
                    help="直近Nヶ月を対象 (--date/--all 未指定時, 既定6)")
    ap.add_argument("--all", action="store_true", help="全レースを対象")
    ap.add_argument("--apply", action="store_true", help="実削除 (既定は dry-run)")
    ap.add_argument("--no-api", action="store_true",
                    help="live odds API を使わない (基準A のみ)")
    ap.add_argument("--repredict", action="store_true",
                    help="削除した日を再予測+再export (--apply 必須)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.date and len(args.date) != 8:
        print(f"❌ 日付フォーマット不正: {args.date}", file=sys.stderr)
        sys.exit(2)

    result = remove_ghosts(
        date=args.date,
        months=args.months,
        all_races=args.all,
        apply=args.apply,
        use_api=not args.no_api,
        verbose=not args.quiet,
    )

    if args.repredict:
        if not args.apply:
            print("⚠️ --repredict は --apply と併用してください (削除してない日は再予測不要)")
        elif result["affected_dates"]:
            repredict_and_export(result["affected_dates"], verbose=not args.quiet)

    sys.exit(0 if result["detected"] == 0 else 1)


if __name__ == "__main__":
    main()
