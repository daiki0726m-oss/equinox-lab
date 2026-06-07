#!/usr/bin/env python3
"""データ完整性 pre-flight check (2026-05-24 導入)

X 投稿・予測・結果取り込み等の前に「データが揃っているか」を確認。
不足があれば自動修復 (fetch_weekend_races / predict 等を発火) する。

過去事故の教訓 (CLAUDE.md 過去のミス事例参照):
- 5/23 R1-R8 欠落で結果反映ゼロ
- 5/24 出走馬未登録(scraper バグ)で予測 0件
- これらは pre-flight でレース件数 / 出走馬件数 / 予測件数を
  チェックすれば事前に検知できた事象。

CLI:
  python scripts/preflight_check.py YYYYMMDD [--auto-fix] [--quiet]

返り値:
  0 = 全項目 OK
  1 = 警告あり (auto-fix で対応済 or 軽微)
  2 = 致命的 (auto-fix 不可)

各 workflow の予測・投稿の直前で呼び出すことを推奨。
"""
from __future__ import annotations
import sys
import os
import argparse
import subprocess

# database import path 解決
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import get_db, init_db  # noqa: E402

# 同 scripts ディレクトリの幽霊馬除去ロジックを再利用 (#43/#44 残存幽霊の検出)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import remove_ghost_horses as rgh  # noqa: E402


# 想定値 (土日開催・3場各12レース)
EXPECTED_RACES_WEEKEND = 30  # 36 が標準だが、開催数で変動するため緩めの最小値
EXPECTED_MIN_RUNNERS_PER_RACE = 5  # 障害戦等で少ない場合あり


def check_races(conn, date_iso: str) -> tuple[int, list[str]]:
    """races テーブルにそのDateのレースが揃っているか"""
    issues = []
    row = conn.execute(
        "SELECT COUNT(*) as c FROM races WHERE race_date = ?", (date_iso,)
    ).fetchone()
    n_races = row["c"] or 0
    if n_races == 0:
        issues.append(f"❌ races テーブルに {date_iso} のレースが 1件もない")
        return 2, issues
    if n_races < EXPECTED_RACES_WEEKEND:
        issues.append(
            f"⚠️ races 件数が少ない: {n_races}件 (期待 {EXPECTED_RACES_WEEKEND}件以上)"
        )
    return (1 if issues else 0), issues


def check_runners(conn, date_iso: str) -> tuple[int, list[str]]:
    """各レースで出走馬が DB に登録されているか"""
    issues = []
    rows = conn.execute(
        """SELECT r.race_id, r.venue, r.race_number,
                  (SELECT COUNT(*) FROM results WHERE race_id = r.race_id) as n_runners
           FROM races r WHERE r.race_date = ?""",
        (date_iso,)
    ).fetchall()
    if not rows:
        return 2, ["❌ races が無いため runners チェック不可"]

    empty = [r for r in rows if (r["n_runners"] or 0) == 0]
    too_few = [r for r in rows if 0 < (r["n_runners"] or 0) < EXPECTED_MIN_RUNNERS_PER_RACE]
    if empty:
        issues.append(
            f"❌ 出走馬未登録レース {len(empty)}件: "
            + ", ".join(f"{r['venue']}{r['race_number']}R" for r in empty[:5])
        )
        return 2, issues
    if too_few:
        issues.append(
            f"⚠️ 出走馬が極端に少ないレース {len(too_few)}件 "
            f"(障害戦等の可能性): "
            + ", ".join(
                f"{r['venue']}{r['race_number']}R({r['n_runners']}頭)" for r in too_few[:5]
            )
        )
    return (1 if issues else 0), issues


def check_predictions(conn, date_iso: str) -> tuple[int, list[str]]:
    """予測キャッシュが揃っているか"""
    issues = []
    row = conn.execute(
        """SELECT COUNT(*) as n FROM predictions_cache pc
           JOIN races r ON pc.race_id = r.race_id
           WHERE r.race_date = ?""",
        (date_iso,)
    ).fetchone()
    n_pred = row["n"] or 0
    n_races_row = conn.execute(
        "SELECT COUNT(*) as c FROM races WHERE race_date = ?", (date_iso,)
    ).fetchone()
    n_races = n_races_row["c"] or 0
    if n_races == 0:
        return 2, ["❌ races 無し"]
    if n_pred == 0:
        issues.append(f"❌ 予測キャッシュ 0件 (races: {n_races}件)")
        return 2, issues
    if n_pred < n_races:
        issues.append(f"⚠️ 予測キャッシュ不足: {n_pred}/{n_races}")
    return (1 if issues else 0), issues


def check_ml_output_sanity(conn, date_iso: str) -> tuple[int, list[str]]:
    """ML 出力の正常性 (#40 sanity gate, 2026-06-07).

    今日 6/7 に発生した「24R 全 D + ◎勝率 5-13% (均等近傍)」事故の再発防止。
    各レースの max(pred_win_pct) と確率分布から「ML が flat に近い出力」を検出。

    判定: 50% 以上のレースで max(pred_win_pct) < 12% なら critical (exit 2)。
          30% 以上なら warning (exit 1)。
    """
    import json as _json
    issues = []
    rows = conn.execute(
        """SELECT pc.race_id, pc.predictions_json
           FROM predictions_cache pc
           JOIN races r ON pc.race_id = r.race_id
           WHERE r.race_date = ?""",
        (date_iso,)
    ).fetchall()
    if not rows:
        return 0, []  # 予測なしは別の check で扱う

    n_total = len(rows)
    n_flat = 0
    flat_details = []
    for r in rows:
        try:
            preds = _json.loads(r["predictions_json"])
        except Exception:
            continue
        if not preds:
            continue
        max_pw = max((p.get("pred_win_pct") or 0) for p in preds)
        if max_pw < 12.0:
            n_flat += 1
            flat_details.append((r["race_id"], max_pw))

    flat_ratio = n_flat / n_total if n_total else 0
    if flat_ratio >= 0.5:
        sample = ", ".join(f"{rid}({mx:.1f}%)" for rid, mx in flat_details[:3])
        issues.append(
            f"❌ ML flat output 検出: {n_flat}/{n_total} レース ({flat_ratio*100:.0f}%) "
            f"で max ◎勝率<12% — 例: {sample}. seal せず再 predict 必須"
        )
        return 2, issues
    elif flat_ratio >= 0.3:
        sample = ", ".join(f"{rid}({mx:.1f}%)" for rid, mx in flat_details[:3])
        issues.append(
            f"⚠️ ML 部分 flat: {n_flat}/{n_total} レース ({flat_ratio*100:.0f}%) "
            f"で max ◎勝率<12% — 例: {sample}"
        )
        return 1, issues
    return 0, []


def check_ghost_horses(conn, date_iso: str, use_api: bool = False) -> tuple[int, list[str], bool]:
    """幽霊馬 (枠順確定前の仮馬番が finish=0 で残存) を検出 (#43/#44)。

    基準(A) 同一レース内 horse_id 重複: 常時チェック (ネットワーク不要・無料)。
            #43 の幽霊は実馬の horse_id を重複させるためこれで大半を検出できる。
    基準(B) DB 出走頭数 vs netkeiba API 実頭数の照合: use_api=True (--ghost-api) 時のみ。
            未開催レース限定で API を呼び、余剰行を検出する。高頻度 cron での
            API 連打を避けるため既定 OFF。

    🚨 finish_position=0 は除外/取消/競走中止の正規値でもあるため、開催済レースの
       finish=0 行は基準(A)の重複以外は触らない (remove_ghost_horses の安全設計に準拠)。

    返り値: (severity, issues, detected)。detected=True なら auto-fix 対象。
    """
    issues: list[str] = []
    date_yyyymmdd = date_iso.replace("-", "")
    races = conn.execute(
        "SELECT race_id, venue, race_number FROM races WHERE race_date = ? OR race_date = ?",
        (date_iso, date_yyyymmdd),
    ).fetchall()
    if not races:
        return 0, issues, False

    flagged = []  # (race, n_ghost, n_db_rows, api_count)
    for race in races:
        rid = race["race_id"]
        rows = rgh._race_rows(conn, rid)
        if not rows:
            continue
        has_finished = any((r.get("finish_position") or 0) > 0 for r in rows)

        api_nums = None
        if use_api and not has_finished:
            api_nums = rgh.fetch_real_horse_numbers(rid)

        ghosts_a, _amb = rgh.detect_criterion_a(rows, api_nums)
        already = {g["horse_number"] for g in ghosts_a}
        ghosts_b = []
        if use_api and not has_finished:
            ghosts_b = rgh.detect_criterion_b(rows, api_nums, already)

        n_ghost = len(ghosts_a) + len(ghosts_b)
        if n_ghost:
            flagged.append((race, n_ghost, len(rows), len(api_nums) if api_nums else None))

    if flagged:
        detail = ", ".join(
            f"{r['venue']}{r['race_number']}R(DB{nr}頭"
            + (f"/API{an}頭" if an is not None else "")
            + f"/幽霊{n})"
            for r, n, nr, an in flagged
        )
        issues.append(f"⚠️ 幽霊馬の疑い {len(flagged)}レース: {detail}")
        return 1, issues, True
    return 0, issues, False


def auto_fix_runners(date_yyyymmdd: str) -> bool:
    """出走馬不足を自動修復 (scrape_shutuba 経由)"""
    print(f"🔧 自動修復: 出走馬を取得中 ({date_yyyymmdd})...")
    try:
        result = subprocess.run(
            ["python3", "-c", f"""
from scraper import NetkeibaScraper
from database import get_db
s = NetkeibaScraper()
ids = s.get_race_list_by_date('{date_yyyymmdd}')
for rid in ids:
    try:
        data = s.scrape_shutuba(rid)
        if data:
            s.save_race_to_db(data)
    except Exception as e:
        print(f'⚠️ {{rid}}: {{e}}')
print(f'✅ {{len(ids)}}レース処理')
"""],
            capture_output=True, text=True, timeout=600
        )
        print(result.stdout[-500:])
        if result.returncode != 0:
            print(f"❌ 自動修復失敗: {result.stderr[-200:]}")
            return False
        return True
    except Exception as e:
        print(f"❌ 自動修復例外: {e}")
        return False


def auto_fix_predictions(date_yyyymmdd: str) -> bool:
    """予測キャッシュ不足を自動修復"""
    print(f"🔧 自動修復: 予測実行中 ({date_yyyymmdd})...")
    try:
        result = subprocess.run(
            ["python3", "predict.py", "predict", "--date", date_yyyymmdd, "--force"],
            capture_output=True, text=True, timeout=900
        )
        if result.returncode != 0:
            print(f"❌ 予測失敗: {result.stderr[-200:]}")
            return False
        print("✅ 予測完了")
        return True
    except Exception as e:
        print(f"❌ 予測例外: {e}")
        return False


def auto_fix_ghosts(date_yyyymmdd: str) -> bool:
    """幽霊馬を除去し、該当日を再予測+再export (remove_ghost_horses 経由)。"""
    print(f"🔧 自動修復: 幽霊馬を除去中 ({date_yyyymmdd})...")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "remove_ghost_horses.py")
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        result = subprocess.run(
            ["python3", script, "--date", date_yyyymmdd, "--apply", "--repredict"],
            cwd=repo, capture_output=True, text=True, timeout=900,
        )
        print(result.stdout[-800:])
        # remove_ghost_horses 返り値: 0=幽霊なし / 1=検出して削除 (どちらも正常)
        if result.returncode in (0, 1):
            return True
        print(f"❌ 幽霊除去失敗: {result.stderr[-200:]}")
        return False
    except Exception as e:
        print(f"❌ 幽霊除去例外: {e}")
        return False


def main():
    ap = argparse.ArgumentParser(description="データ完整性 pre-flight check")
    ap.add_argument("date", help="YYYYMMDD")
    ap.add_argument("--auto-fix", action="store_true", help="不足を自動修復")
    ap.add_argument("--quiet", action="store_true", help="OK時は出力しない")
    ap.add_argument("--ghost-api", action="store_true",
                    help="幽霊馬チェックで netkeiba オッズ API も照合 (基準B)。"
                         "高頻度 cron での連打を避けるため既定 OFF。"
                         "OFF でも基準A(horse_id重複)は常時チェック")
    args = ap.parse_args()

    if len(args.date) != 8:
        print(f"❌ 日付フォーマット不正: {args.date}", file=sys.stderr)
        sys.exit(2)

    date_iso = f"{args.date[:4]}-{args.date[4:6]}-{args.date[6:8]}"
    init_db()

    all_issues = []
    overall_severity = 0  # 0=OK, 1=warn, 2=critical

    with get_db() as conn:
        # 1. races
        sev, issues = check_races(conn, date_iso)
        overall_severity = max(overall_severity, sev)
        all_issues.extend(issues)

        # 2. runners
        sev, issues = check_runners(conn, date_iso)
        overall_severity = max(overall_severity, sev)
        all_issues.extend(issues)
        runners_critical = sev == 2

        # 3. predictions
        sev, issues = check_predictions(conn, date_iso)
        overall_severity = max(overall_severity, sev)
        all_issues.extend(issues)
        pred_critical = sev == 2

        # 3.5. ML 出力 sanity (#40, 2026-06-07): flat / degenerate prediction の早期検知
        # 「24R 全 D + ◎勝率 5-13%」のような ML 異常を seal 前に検出して
        # auto-fix の re-predict を発動させる
        sev, issues = check_ml_output_sanity(conn, date_iso)
        overall_severity = max(overall_severity, sev)
        all_issues.extend(issues)
        ml_critical = sev == 2

        # 4. 幽霊馬 (#43/#44) — 基準A は無料・常時、基準B(API) は --ghost-api 時のみ
        sev, issues, ghost_detected = check_ghost_horses(conn, date_iso, use_api=args.ghost_api)
        overall_severity = max(overall_severity, sev)
        all_issues.extend(issues)

    # Auto-fix
    if args.auto_fix and overall_severity == 2:
        print(f"\n🛠 Auto-fix mode: 致命的問題を修復します...\n")
        if runners_critical:
            if auto_fix_runners(args.date):
                print("✅ 出走馬の自動修復成功")
            else:
                print("❌ 出走馬の自動修復失敗")
        if pred_critical or runners_critical or ml_critical:
            # 出走馬入ったら予測も再実行。ML flat output も再 predict で修復試行
            if ml_critical:
                print("🔄 ML flat output → predict --force で再予測")
            if auto_fix_predictions(args.date):
                print("✅ 予測の自動修復成功")
                overall_severity = 0
            else:
                print("❌ 予測の自動修復失敗")

    # 幽霊馬 auto-fix (警告レベルでも除去する。除去後は該当日を再予測+再export)
    if args.auto_fix and ghost_detected:
        print(f"\n🛠 Auto-fix mode: 幽霊馬を除去します...\n")
        if auto_fix_ghosts(args.date):
            print("✅ 幽霊馬の自動修復成功")
        else:
            print("❌ 幽霊馬の自動修復失敗")

    if not args.quiet or all_issues:
        print(f"\n━━━━━━ Pre-flight check: {args.date} ━━━━━━")
        if all_issues:
            for iss in all_issues:
                print(f"  {iss}")
        else:
            print("✅ 全項目 OK")
        print(f"\n総合: {'✅ OK' if overall_severity == 0 else '⚠️ 警告' if overall_severity == 1 else '❌ 致命的'}")

    sys.exit(overall_severity)


if __name__ == "__main__":
    main()
