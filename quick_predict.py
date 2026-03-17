"""
🏇 リアルタイム簡易予測スクリプト
出馬表 → 各馬の過去成績をスクレイピング → 簡易分析 → 予測
MLモデル不要・データベース不要で即座に使える
"""

import sys
import re
import time
import requests
from bs4 import BeautifulSoup

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
})
INTERVAL = 1.2


def fetch(url, encoding="EUC-JP"):
    time.sleep(INTERVAL)
    try:
        r = SESSION.get(url, timeout=20)
        r.encoding = encoding
        return r if r.status_code == 200 else None
    except Exception as e:
        print(f"  ⚠️ {e}")
        return None


def parse_time(t):
    """'1:12.6' → 72.6"""
    if not t:
        return 0
    try:
        if ":" in t:
            parts = t.split(":")
            return int(parts[0]) * 60 + float(parts[1])
        return float(t)
    except:
        return 0


def get_shutuba(race_id):
    """出馬表を取得"""
    url = f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"
    resp = fetch(url)
    if not resp:
        return None, []

    soup = BeautifulSoup(resp.text, "lxml")

    # レース情報
    race_info = {}
    rn = soup.find("h1", class_="RaceName") or soup.find("div", class_="RaceName")
    race_info["name"] = rn.get_text(strip=True) if rn else ""

    rd01 = soup.find("div", class_="RaceData01")
    if rd01:
        txt = rd01.get_text()
        m = re.search(r"(\d{3,4})m", txt)
        race_info["distance"] = int(m.group(1)) if m else 0
        race_info["surface"] = "芝" if "芝" in txt else ("ダート" if "ダ" in txt else "")
        race_info["condition"] = ""
        for c in ["不良", "稍重", "重", "良"]:
            if c in txt:
                race_info["condition"] = c
                break

    rd02 = soup.find("div", class_="RaceData02")
    if rd02:
        txt = rd02.get_text()
        for v in ["札幌","函館","福島","新潟","東京","中山","中京","京都","阪神","小倉"]:
            if v in txt:
                race_info["venue"] = v
                break

    # 出走馬
    entries = []
    table = None
    for t in soup.find_all("table"):
        ths = [th.get_text(strip=True) for th in t.find_all("th")]
        if "馬名" in ths or any("馬" in s for s in ths):
            table = t
            break

    if not table:
        return race_info, []

    for row in table.find_all("tr")[1:]:
        cols = row.find_all("td")
        if len(cols) < 7:
            continue
        umaban = cols[1].get_text(strip=True)
        horse_a = cols[3].find("a")
        horse_name = horse_a.get_text(strip=True) if horse_a else ""
        horse_id = ""
        if horse_a and horse_a.get("href"):
            m = re.search(r"/horse/(\w+)", horse_a["href"])
            if m:
                horse_id = m.group(1)
        sex_age = cols[4].get_text(strip=True)
        impost = cols[5].get_text(strip=True)
        jockey_a = cols[6].find("a")
        jockey = jockey_a.get_text(strip=True) if jockey_a else cols[6].get_text(strip=True)

        entries.append({
            "umaban": int(umaban) if umaban.isdigit() else 0,
            "name": horse_name, "horse_id": horse_id,
            "sex_age": sex_age, "impost": impost, "jockey": jockey,
        })

    return race_info, entries


def get_horse_history(horse_id, max_races=8):
    """馬の過去成績を取得
    db.netkeiba.com/horse/result/{id}/ のカラム構成:
    [0]=日付 [1]=開催 [2]=天気 [3]=R [4]=レース名 [5]=映像 [6]=頭数
    [7]=枠番 [8]=馬番 [9]=オッズ [10]=人気 [11]=着順 [12]=騎手
    [13]=斤量 [14]=距離 [15]=水分量 [16]=馬場 [17]=馬場指数
    [18]=タイム [19]=着差 [20]=タイム指数 [21]=通過 [22]=ペース
    [23]=上り [24]=馬体重
    """
    url = f"https://db.netkeiba.com/horse/result/{horse_id}/"
    resp = fetch(url)
    if not resp:
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    perf = soup.find("table", class_="db_h_race_results")
    if not perf:
        for t in soup.find_all("table"):
            ths = [th.get_text(strip=True) for th in t.find_all("th")]
            if "着順" in ths or "日付" in ths:
                perf = t
                break

    if not perf:
        return []

    history = []
    rows = perf.find_all("tr")[1:max_races+1]
    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 24:
            continue
        try:
            r = {}
            r["date"] = cols[0].get_text(strip=True)
            r["venue"] = cols[1].get_text(strip=True)
            r["race_name"] = cols[4].get_text(strip=True)[:12]

            # [14]=距離 (例: "ダ1400")
            dist_txt = cols[14].get_text(strip=True)
            dm = re.search(r"([芝ダ障])(\d{3,4})", dist_txt)
            r["surface"] = dm.group(1) if dm else ""
            r["distance"] = int(dm.group(2)) if dm else 0

            # [16]=馬場状態
            r["condition"] = cols[16].get_text(strip=True)

            # [11]=着順
            pos = cols[11].get_text(strip=True)
            r["finish"] = int(pos) if pos.isdigit() else 99

            # [6]=頭数
            fc = cols[6].get_text(strip=True)
            r["field_count"] = int(fc) if fc.isdigit() else 0

            # [18]=タイム
            time_txt = cols[18].get_text(strip=True)
            r["time_sec"] = parse_time(time_txt)

            # [23]=上がり3F
            l3f = cols[23].get_text(strip=True)
            r["last_3f"] = float(l3f) if l3f.replace(".", "").isdigit() else 0

            # [21]=通過順
            r["passing"] = cols[21].get_text(strip=True)

            # [10]=人気
            pop = cols[10].get_text(strip=True)
            r["popularity"] = int(pop) if pop.isdigit() else 0

            history.append(r)
        except:
            continue

    return history


def calc_speed_index(hist, target_dist, target_surface):
    """簡易スピード指数（距離補正付き）"""
    indices = []
    for r in hist:
        if r["time_sec"] <= 0 or r["distance"] <= 0:
            continue
        # 同じ馬場のみ
        if target_surface and r["surface"] and r["surface"] != target_surface[0]:
            continue
        # 1Fあたりの秒数
        pace_per_f = r["time_sec"] / (r["distance"] / 200)
        # 距離差による補正（長距離ほど1Fあたりは遅い）
        dist_diff = abs(r["distance"] - target_dist) / 200
        # 基本指数 = 100 - (1Fペース - 基準ペース) * 10
        base_pace = 12.0 if target_surface == "芝" else 12.5
        si = 100 - (pace_per_f - base_pace) * 10
        # 距離差ペナルティ
        si -= dist_diff * 0.5
        # 馬場補正
        if r["condition"] in ["重", "不良"]:
            si += 2  # 重馬場は時計がかかるので加点
        indices.append(si)

    if not indices:
        return 0, 0
    return round(max(indices), 1), round(sum(indices)/len(indices), 1)


def analyze_running_style(hist):
    """脚質判定"""
    styles = {"逃": 0, "先": 0, "差": 0, "追": 0}
    for r in hist:
        p = r.get("passing", "")
        nums = re.findall(r"\d+", p)
        if not nums or r.get("field_count", 0) <= 0:
            continue
        first_pos = int(nums[0])
        ratio = first_pos / r["field_count"]
        if ratio <= 0.15:
            styles["逃"] += 1
        elif ratio <= 0.4:
            styles["先"] += 1
        elif ratio <= 0.7:
            styles["差"] += 1
        else:
            styles["追"] += 1
    total = sum(styles.values())
    if total == 0:
        return "不明"
    best = max(styles, key=styles.get)
    return best + "行" if best == "先" else best + "げ" if best == "逃" else best + "し" if best == "差" else best + "込"


def analyze_distance_fit(hist, target_dist):
    """距離適性（近い距離での成績）"""
    near = [r for r in hist if abs(r.get("distance", 0) - target_dist) <= 200]
    if not near:
        return 0, 0
    top3 = sum(1 for r in near if r["finish"] <= 3)
    return top3, len(near)


def analyze_last_3f(hist):
    """上がり3F分析"""
    vals = [r["last_3f"] for r in hist if r.get("last_3f", 0) > 0]
    if not vals:
        return 0, 0
    return round(min(vals), 1), round(sum(vals)/len(vals), 1)


def predict_race(race_id):
    """レースを分析・予測"""
    print(f"\n📡 出馬表を取得中... {race_id}")
    race_info, entries = get_shutuba(race_id)

    if not entries:
        print("❌ 出馬表が取得できませんでした")
        return

    dist = race_info.get("distance", 0)
    surf = race_info.get("surface", "")
    venue = race_info.get("venue", "")

    print(f"\n{'='*65}")
    print(f"🏇 {venue} {race_info.get('name','')} {surf}{dist}m {race_info.get('condition','')}")
    print(f"{'='*65}")

    # 各馬の分析
    results = []
    for e in entries:
        if not e["horse_id"]:
            continue
        print(f"  📊 {e['umaban']:>2}番 {e['name']} の過去成績を取得中...")
        hist = get_horse_history(e["horse_id"])

        si_max, si_avg = calc_speed_index(hist, dist, surf)
        style = analyze_running_style(hist)
        dist_top3, dist_runs = analyze_distance_fit(hist, dist)
        l3f_best, l3f_avg = analyze_last_3f(hist)

        # 前走成績
        last_race = hist[0] if hist else {}
        last_finish = last_race.get("finish", 99)
        last_pop = last_race.get("popularity", 0)

        # 総合スコア計算
        score = 0
        score += si_avg * 0.4           # スピード指数（平均）
        score += si_max * 0.2           # スピード指数（最高）
        if l3f_avg > 0:
            score += (40 - l3f_avg) * 2  # 上がり3Fが速いほど加点
        if dist_runs > 0:
            score += (dist_top3 / dist_runs) * 15  # 距離適性
        if last_finish <= 3:
            score += 5                   # 前走好走ボーナス
        if last_finish == 1:
            score += 3                   # 前走1着ボーナス

        results.append({
            **e,
            "si_max": si_max, "si_avg": si_avg,
            "style": style,
            "dist_fit": f"{dist_top3}/{dist_runs}",
            "l3f_best": l3f_best, "l3f_avg": l3f_avg,
            "last": f"{last_finish}着" if last_finish < 99 else "?",
            "last_pop": last_pop,
            "score": round(score, 1),
            "hist_count": len(hist),
        })

    # スコア順にソート
    results.sort(key=lambda x: x["score"], reverse=True)

    # 表示
    print(f"\n{'─'*65}")
    print(f"{'順':>2} {'番':>3} {'馬名':<12} {'SI平':>5} {'SI最':>5} "
          f"{'上3F':>5} {'脚質':>4} {'距離':>4} {'前走':>4} {'スコア':>6}")
    print(f"{'─'*65}")

    for i, r in enumerate(results):
        mark = "◎" if i == 0 else "○" if i == 1 else "▲" if i == 2 else "△" if i <= 4 else "  "
        print(f"{mark} {r['umaban']:>3} {r['name']:<12} "
              f"{r['si_avg']:>5.1f} {r['si_max']:>5.1f} "
              f"{r['l3f_avg']:>5.1f} {r['style']:>4} "
              f"{r['dist_fit']:>4} {r['last']:>4} "
              f"{r['score']:>6.1f}")

    # 馬券提案
    if len(results) >= 3:
        top = results[:5]
        print(f"\n{'='*65}")
        print(f"💰 推奨馬券（予算¥1,000）")
        print(f"{'='*65}")

        r1, r2, r3 = results[0], results[1], results[2]
        print(f"\n  ◎ {r1['umaban']}番 {r1['name']} (スコア{r1['score']})")
        print(f"  ○ {r2['umaban']}番 {r2['name']} (スコア{r2['score']})")
        print(f"  ▲ {r3['umaban']}番 {r3['name']} (スコア{r3['score']})")

        # スコア差が小さい = 混戦 → ワイドBOX
        score_gap = results[0]["score"] - results[2]["score"]
        if score_gap < 3:
            print(f"\n  📊 上位の差が小さい → 混戦模様")
            print(f"\n  【ワイドBOX】⑨{r1['umaban']}-⑨{r2['umaban']}-⑨{r3['umaban']}")
            print(f"    → 3点 × ¥200 = ¥600")
            if len(results) > 3:
                r4 = results[3]
                print(f"  【ワイド】{r1['umaban']}-{r4['umaban']}  ¥200")
                print(f"  【複勝】{r1['umaban']}  ¥200")
            print(f"  合計: ¥1,000")
        else:
            print(f"\n  📊 ◎が抜けている → 三連単フォーメーション")
            others = [r['umaban'] for r in results[1:5]]
            print(f"\n  【三連単フォーメーション】")
            print(f"    1着: {r1['umaban']}")
            print(f"    2着: {', '.join(str(x) for x in others[:3])}")
            print(f"    3着: {', '.join(str(x) for x in others[:4])}")
            combos = 3 * 4 - 3  # 2着×3着 - 重複
            cost = min(combos, 10) * 100
            print(f"    → {min(combos,10)}点 × ¥100 = ¥{cost}")
            remaining = 1000 - cost
            if remaining > 0:
                print(f"  【複勝】{r1['umaban']}  ¥{remaining}")
            print(f"  合計: ¥1,000")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("🏇 リアルタイム簡易予測")
        print("使い方: python quick_predict.py <race_id>")
        print("例:     python quick_predict.py 202606020512")
        sys.exit(0)

    race_id = sys.argv[1]
    predict_race(race_id)
