#!/usr/bin/env python3
"""#90: predictions_cache の文字化け馬名を『馬番→正しい名』で包括修復する。

cache には 2系統の馬名スナップショットがある:
- predictions_json: 各馬の horse_name (馬リスト表示用)
- all_bets_json: 買い目の horse_name (「軸-相手」の組み合わせ文字列。horse_numbers も保持)
どちらも predict 実行時 (#85/#90 修正前の朝) の化け名が埋まっており、horses を直しても残る。

対策: 対象レースの出馬表を修正済 scraper で引き直し『馬番→正しい名』map を作り、
(1) horses テーブルを更新、(2) predictions_json の各馬名を馬番で置換、
(3) all_bets_json の horse_name を horse_numbers から組み立て直す。
→ 化け文字列の形に依存せず、馬番ベースで確実に直す。実行後 export_predictions で再書き出し。

使い方: python3 scripts/fix_garbled_cache_names.py YYYY-MM-DD [YYYY-MM-DD ...]
"""
import sys
import json
import time

sys.path.insert(0, ".")
from database import get_db  # noqa: E402
from scraper import NetkeibaScraper  # noqa: E402

FFFD = chr(65533)


def main(dates):
    with get_db() as conn:
        ph = ",".join("?" * len(dates))
        rows = conn.execute(
            f"""SELECT pc.race_id, pc.predictions_json, pc.all_bets_json
                FROM predictions_cache pc JOIN races r ON r.race_id = pc.race_id
                WHERE r.race_date IN ({ph})""", dates).fetchall()
    need = [r for r in rows if (r[1] and FFFD in r[1]) or (r[2] and FFFD in r[2])]
    print(f"🔧 化けを含むレース {len(need)}件を修復")

    scraper = NetkeibaScraper()
    fixed_pj = fixed_ab = 0
    for race_id, pj, ab in need:
        # 出馬表を引き直して『馬番→正しい名』
        num2name = {}
        try:
            data = scraper.scrape_shutuba(race_id)
        except Exception as e:
            print(f"  ⚠️ {race_id}: scrape失敗 {e}")
            data = None
        if data:
            for e in data.get("entries", []):
                n = e.get("horse_number")
                nm = (e.get("horse_name") or "").strip()
                if n and nm and FFFD not in nm:
                    num2name[n] = nm
        with get_db() as conn:
            # horses テーブルも直す (export の他 join 用)
            for e in (data.get("entries", []) if data else []):
                hid = e.get("horse_id")
                nm = (e.get("horse_name") or "").strip()
                if hid and nm and FFFD not in nm:
                    conn.execute(
                        "UPDATE horses SET horse_name=? WHERE horse_id=? AND "
                        "(horse_name LIKE '%'||CHAR(65533)||'%' OR horse_name='' OR horse_name IS NULL)",
                        (nm, hid))
            # (2) predictions_json: 馬番で名前置換
            new_pj = pj
            full = {}
            if pj:
                d = json.loads(pj)
                for h in d:
                    n = h.get("horse_number")
                    if n in num2name:
                        if FFFD in str(h.get("horse_name", "")):
                            h["horse_name"] = num2name[n]
                            fixed_pj += 1
                    nm = h.get("horse_name")
                    if n and nm and FFFD not in str(nm):
                        full[n] = nm
                full.update(num2name)
                new_pj = json.dumps(d, ensure_ascii=False)
            # (3) all_bets_json: horse_numbers から組み立て直す
            new_ab = ab
            if ab:
                abd = json.loads(ab)
                for _bt, bets in abd.items():
                    for b in bets:
                        nums = b.get("horse_numbers", [])
                        names = [full.get(x) for x in nums]
                        if nums and all(names):
                            joined = "-".join(names)
                            if joined != b.get("horse_name"):
                                b["horse_name"] = joined
                                fixed_ab += 1
                new_ab = json.dumps(abd, ensure_ascii=False)
            conn.execute(
                "UPDATE predictions_cache SET predictions_json=?, all_bets_json=? WHERE race_id=?",
                (new_pj, new_ab, race_id))
        time.sleep(1)
    print(f"✅ predictions_json 名修復 {fixed_pj}件 / all_bets 組替 {fixed_ab}件")

    with get_db() as conn:
        ph = ",".join("?" * len(dates))
        remain = conn.execute(
            f"""SELECT COUNT(*) FROM predictions_cache pc JOIN races r ON r.race_id=pc.race_id
                WHERE r.race_date IN ({ph})
                  AND (pc.predictions_json LIKE '%'||CHAR(65533)||'%'
                       OR pc.all_bets_json LIKE '%'||CHAR(65533)||'%')""", dates).fetchone()[0]
    print(f"   残り化けレース: {remain}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print("使い方: fix_garbled_cache_names.py YYYY-MM-DD [...]")
        sys.exit(1)
    main(args)
