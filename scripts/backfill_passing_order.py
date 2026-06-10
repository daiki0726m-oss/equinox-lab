#!/usr/bin/env python3
"""通過順 (passing_order) の系統的 backfill (#65)。

旧 seed が race.netkeiba.com の result.html を使っており、過去レースでは
「通過」列が無いため 2021-2023 がほぼ全滅していた。db.netkeiba.com の
アーカイブページ (通過・上り常設) から補完する。

usage: python3 scripts/backfill_passing_order.py 2021 2022 2023
"""
import sys, time, sqlite3
from scraper import NetkeibaScraper

def main():
    years = sys.argv[1:] or ['2021','2022','2023']
    s = NetkeibaScraper()
    c = sqlite3.connect('keiba.db')
    for y in years:
        rids = [r[0] for r in c.execute("""
            SELECT DISTINCT ra.race_id FROM races ra JOIN results res ON ra.race_id=res.race_id
            WHERE ra.race_date LIKE ?||'%' AND (res.passing_order IS NULL OR res.passing_order='')
              AND res.finish_position>0 ORDER BY ra.race_id""",(y,))]
        print(f"=== {y}: {len(rids)} races に欠落 ===", flush=True)
        done = fixed = 0
        for rid in rids:
            try:
                d = s.scrape_race_result_archive(rid)
                for r in (d.get('results') if d else []) or []:
                    po = r.get('passing_order') or ''
                    if po:
                        cur = c.execute("""UPDATE results SET passing_order=?,
                            last_3f=CASE WHEN (last_3f IS NULL OR last_3f=0) AND ?>0 THEN ? ELSE last_3f END
                            WHERE race_id=? AND horse_number=? AND (passing_order IS NULL OR passing_order='')""",
                            (po, r.get('last_3f') or 0, r.get('last_3f') or 0, rid, r.get('horse_number')))
                        fixed += cur.rowcount
            except Exception as e:
                print(f"  ⚠️ {rid}: {e}", flush=True)
            done += 1
            if done % 50 == 0:
                c.commit()
                print(f"  {y}: {done}/{len(rids)} races / {fixed} rows", flush=True)
            time.sleep(1.0)
        c.commit()
        print(f"=== {y} 完了: {fixed} rows 補完 ===", flush=True)

if __name__ == '__main__':
    main()
