"""
netkeibaスクレイパー
レース情報・出馬表・結果・馬情報をスクレイピング
"""

import re
import time
import requests
from bs4 import BeautifulSoup
import pandas as pd
from database import get_db, init_db


class NetkeibaScraper:
    """netkeibaからデータを収集するスクレイパー"""

    BASE_URL = "https://race.netkeiba.com"
    DB_URL = "https://db.netkeiba.com"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36"
    }
    REQUEST_INTERVAL = 1.5  # リクエスト間隔(秒)

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)
        init_db()

    def _get(self, url, encoding=None):
        """GETリクエスト（間隔制御付き）"""
        time.sleep(self.REQUEST_INTERVAL)
        try:
            resp = self.session.get(url, timeout=30)
            resp.encoding = encoding or resp.apparent_encoding
            if resp.status_code == 200:
                return resp
            print(f"⚠️ HTTP {resp.status_code}: {url}")
            return None
        except requests.RequestException as e:
            print(f"❌ リクエストエラー: {e}")
            return None

    # =========================================================
    # レース一覧の取得
    # =========================================================
    def get_race_list(self, year, month):
        """指定年月のレースID一覧を取得（カレンダー→日別→race_id）"""
        # Step 1: カレンダーから開催日一覧を取得
        calendar_url = f"{self.BASE_URL}/top/calendar.html?year={year}&month={month}"
        resp = self._get(calendar_url)
        if not resp:
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        dates = re.findall(r"kaisai_date=(\d{8})", resp.text)
        dates = sorted(set(dates))

        if not dates:
            print(f"📋 {year}年{month}月: 開催日なし")
            return []

        print(f"📅 {year}年{month}月: {len(dates)}開催日")

        # Step 2: 各開催日のレースIDを取得
        all_race_ids = []
        for date_str in dates:
            race_ids = self.get_race_list_by_date(date_str)
            all_race_ids.extend(race_ids)

        print(f"📋 {year}年{month}月: 合計{len(all_race_ids)}レース取得")
        return all_race_ids

    def get_race_list_by_date(self, date_str):
        """指定日のレースID一覧を取得 (date_str: 'YYYYMMDD')"""
        url = f"{self.BASE_URL}/top/race_list_sub.html?kaisai_date={date_str}"
        resp = self._get(url)
        if not resp:
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        race_ids = []

        # race_id=XXXXXXXXXXXX の形式でクエリパラメータから取得
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            match = re.search(r"race_id=(\d{12})", href)
            if match:
                race_id = match.group(1)
                # result.html のリンクのみ（movie.html は重複なので除外）
                if "result.html" in href or "shutuba.html" in href:
                    if race_id not in race_ids:
                        race_ids.append(race_id)

        return race_ids

    # =========================================================
    # レース結果の取得・パース
    # =========================================================
    def scrape_race_result(self, race_id):
        """1つのレース結果ページをスクレイピング"""
        url = f"{self.BASE_URL}/race/result.html?race_id={race_id}"
        resp = self._get(url, encoding="EUC-JP")
        if not resp:
            return None

        soup = BeautifulSoup(resp.text, "lxml")
        race_data = {"race_id": race_id}

        # レース情報のパース
        race_data.update(self._parse_race_info(soup, race_id))

        # 結果テーブルのパース
        results = self._parse_result_table(soup, race_id)
        race_data["results"] = results

        # 配当テーブルのパース
        payouts = self._parse_payout_table(soup, race_id)
        race_data["payouts"] = payouts

        return race_data

    def _parse_race_info(self, soup, race_id):
        """レース基本情報をパース"""
        info = {
            "race_date": "",
            "venue": "",
            "race_number": 0,
            "race_name": "",
            "grade": "",
            "distance": 0,
            "surface": "",
            "direction": "",
            "weather": "",
            "track_condition": "",
            "horse_count": 0,
            "start_time": "",
        }

        # レース名
        race_name_tag = soup.find("h1", class_="RaceName")
        if not race_name_tag:
            race_name_tag = soup.find("div", class_="RaceName")
        if race_name_tag:
            info["race_name"] = race_name_tag.get_text(strip=True)

        # レース詳細 (距離・馬場・天候等)
        # RaceData01 は <div>, <dl>, <span> など様々な形式がありえる
        race_data01 = soup.find("div", class_="RaceData01") or soup.find("dl", class_="RaceData01")
        if race_data01:
            text = race_data01.get_text()

            # 距離 (3桁 or 4桁 + m)
            dist_match = re.search(r"(\d{3,4})m", text)
            if dist_match:
                info["distance"] = int(dist_match.group(1))

            # 芝/ダート
            if "芝" in text:
                info["surface"] = "芝"
            elif "ダ" in text:
                info["surface"] = "ダート"
            elif "障" in text:
                info["surface"] = "障害"

            # 右/左
            if "右" in text:
                info["direction"] = "右"
            elif "左" in text:
                info["direction"] = "左"

            # 天候
            weather_match = re.search(r"天候:(\S+)", text)
            if weather_match:
                info["weather"] = weather_match.group(1)

            # 馬場状態
            condition_match = re.search(r"馬場:(\S+)", text)
            if condition_match:
                info["track_condition"] = condition_match.group(1)
            else:
                for cond in ["不良", "稍重", "重", "良"]:
                    if cond in text:
                        info["track_condition"] = cond
                        break

            # 発走時刻 (例: "15:40発走" or 時刻パターン "10:05")
            time_match = re.search(r"(\d{1,2}:\d{2})", text)
            if time_match:
                info["start_time"] = time_match.group(1)

        # 日付（titleタグから取得）
        title_tag = soup.find("title")
        if title_tag:
            date_match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", title_tag.get_text())
            if date_match:
                info["race_date"] = f"{date_match.group(1)}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"

        # 開催情報
        race_data02 = soup.find("div", class_="RaceData02") or soup.find("dl", class_="RaceData02")
        if race_data02:
            text = race_data02.get_text()
            # 競馬場名
            venues = ["札幌", "函館", "福島", "新潟", "東京", "中山", "中京", "京都", "阪神", "小倉"]
            for v in venues:
                if v in text:
                    info["venue"] = v
                    break

        # race_idからの情報補完
        if len(race_id) == 12:
            info["race_number"] = int(race_id[10:12])
            if not info["venue"]:
                venue_codes = {
                    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟",
                    "05": "東京", "06": "中山", "07": "中京", "08": "京都",
                    "09": "阪神", "10": "小倉"
                }
                info["venue"] = venue_codes.get(race_id[4:6], "不明")

        return info

    def _parse_result_table(self, soup, race_id):
        """
        結果テーブルをパース

        netkeiba result.html のカラム構成 (2024年版):
        col[0]:  着順
        col[1]:  枠番
        col[2]:  馬番
        col[3]:  馬名 (リンク→horse_id)
        col[4]:  性齢
        col[5]:  斤量
        col[6]:  騎手 (リンク→jockey_id)
        col[7]:  タイム
        col[8]:  着差
        col[9]:  人気
        col[10]: 単勝オッズ
        col[11]: 上がり3F
        col[12]: 通過順
        col[13]: 調教師 (リンク→trainer_id)
        col[14]: 馬体重(増減)
        """
        results = []

        # テーブル検索: 複数のセレクタを試す
        table = None
        for selector in [
            ("table", {"class_": "RaceTable01"}),
            ("table", {"class_": "Shutuba_Table"}),
            ("table", {"summary": "レース結果"}),
        ]:
            table = soup.find(selector[0], **selector[1])
            if table:
                break

        if not table:
            # フォールバック: ヘッダーに「着順」か「着」を含むテーブルを探す
            for t in soup.find_all("table"):
                ths = t.find_all("th")
                texts = [th.get_text(strip=True) for th in ths]
                if any("着" in txt for txt in texts):
                    table = t
                    break

        if not table:
            print(f"⚠️ 結果テーブルが見つかりません: {race_id}")
            return results

        rows = table.find_all("tr")[1:]  # ヘッダーをスキップ

        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 13:
                continue

            entry = {"race_id": race_id}

            try:
                # col[0]: 着順
                pos_text = cols[0].get_text(strip=True)
                entry["finish_position"] = int(pos_text) if pos_text.isdigit() else 0

                # col[1]: 枠番
                post_text = cols[1].get_text(strip=True)
                entry["post_position"] = int(post_text) if post_text.isdigit() else 0

                # col[2]: 馬番
                num_text = cols[2].get_text(strip=True)
                entry["horse_number"] = int(num_text) if num_text.isdigit() else 0

                # col[3]: 馬名 + horse_id
                horse_tag = cols[3].find("a")
                entry["horse_name"] = cols[3].get_text(strip=True)
                entry["horse_id"] = ""
                if horse_tag and horse_tag.get("href"):
                    h_match = re.search(r"/horse/(\w+)", horse_tag["href"])
                    if h_match:
                        entry["horse_id"] = h_match.group(1)

                # col[4]: 性齢
                sex_age = cols[4].get_text(strip=True)
                entry["sex"] = sex_age[0] if sex_age else ""
                entry["age"] = int(sex_age[1:]) if len(sex_age) > 1 and sex_age[1:].isdigit() else 0

                # col[5]: 斤量
                impost_text = cols[5].get_text(strip=True)
                entry["impost"] = float(impost_text) if self._is_number(impost_text) else 0

                # col[6]: 騎手 + jockey_id
                jockey_tag = cols[6].find("a")
                entry["jockey_name"] = cols[6].get_text(strip=True)
                entry["jockey_id"] = ""
                if jockey_tag and jockey_tag.get("href"):
                    j_match = re.search(r"/jockey/(?:result/recent/)?(\w+)", jockey_tag["href"])
                    if j_match:
                        entry["jockey_id"] = j_match.group(1)

                # col[7]: タイム
                time_text = cols[7].get_text(strip=True)
                entry["finish_time"] = time_text
                entry["finish_time_seconds"] = self._parse_time(time_text)

                # col[8]: 着差
                entry["margin"] = cols[8].get_text(strip=True)

                # col[9]: 人気
                pop_text = cols[9].get_text(strip=True) if len(cols) > 9 else ""
                entry["popularity"] = int(pop_text) if pop_text.isdigit() else 0

                # col[10]: 単勝オッズ
                odds_text = cols[10].get_text(strip=True) if len(cols) > 10 else ""
                entry["odds"] = float(odds_text) if self._is_number(odds_text) else 0

                # col[11]: 上がり3F
                last3f_text = cols[11].get_text(strip=True) if len(cols) > 11 else ""
                entry["last_3f"] = float(last3f_text) if self._is_number(last3f_text) else 0

                # col[12]: 通過順
                entry["passing_order"] = cols[12].get_text(strip=True) if len(cols) > 12 else ""

                # col[13]: 調教師 + trainer_id
                entry["trainer_name"] = ""
                entry["trainer_id"] = ""
                if len(cols) > 13:
                    trainer_tag = cols[13].find("a")
                    entry["trainer_name"] = cols[13].get_text(strip=True)
                    if trainer_tag and trainer_tag.get("href"):
                        t_match = re.search(r"/trainer/(?:result/recent/)?(\w+)", trainer_tag["href"])
                        if t_match:
                            entry["trainer_id"] = t_match.group(1)

                # col[14]: 馬体重(増減)
                entry["weight"] = 0
                entry["weight_change"] = 0
                if len(cols) > 14:
                    weight_text = cols[14].get_text(strip=True)
                    w_match = re.match(r"(\d+)\(([+-]?\d+)\)", weight_text)
                    if w_match:
                        entry["weight"] = int(w_match.group(1))
                        entry["weight_change"] = int(w_match.group(2))
                    elif weight_text.isdigit():
                        entry["weight"] = int(weight_text)

                results.append(entry)

            except (ValueError, IndexError) as e:
                print(f"⚠️ パースエラー (行スキップ): {e}")
                continue

        return results

    def _parse_payout_table(self, soup, race_id):
        """配当テーブルをパース

        netkeiba result.html の払戻テーブル構造 (2024-2026版):
        - Payout_Detail_Table が2つ:
          Table1: 単勝 | 複勝 | 枠連 | 馬連
          Table2: ワイド | 馬単 | 3連複 | 3連単
        - 各行: TH(券種名) + TD.Result(組合せ) + TD.Payout(配当) + TD.Ninki(人気)
        - 複勝/ワイドは1行に複数エントリ (br区切り)
        """
        payouts = []

        # 2つの Payout_Detail_Table を全て取得
        payout_tables = soup.find_all("table", class_="Payout_Detail_Table")
        if not payout_tables:
            return payouts

        # 券種名の正規化マップ
        type_norm = {
            "単勝": "単勝", "複勝": "複勝", "枠連": "枠連",
            "馬連": "馬連", "ワイド": "ワイド", "馬単": "馬単",
            "3連複": "三連複", "三連複": "三連複",
            "3連単": "三連単", "三連単": "三連単",
        }

        for table in payout_tables:
            for row in table.find_all("tr"):
                ths = row.find_all("th")
                tds = row.find_all("td")
                if not ths or len(tds) < 2:
                    continue

                th_text = ths[0].get_text(strip=True)
                bet_type = type_norm.get(th_text)
                if not bet_type:
                    continue

                combo_td = tds[0]
                payout_td = tds[1]
                pop_td = tds[2] if len(tds) > 2 else None

                try:
                    # === 組み合わせの抽出 ===
                    combos = self._extract_combos(combo_td, bet_type)

                    # === 配当金の抽出 (br区切り) ===
                    payout_vals = self._extract_payout_values(payout_td)

                    # === 人気の抽出 (複数span) ===
                    pop_vals = self._extract_popularity(pop_td)

                    # 結合
                    for i, combo in enumerate(combos):
                        if not combo:
                            continue
                        payout_val = payout_vals[i] if i < len(payout_vals) else 0
                        pop_val = pop_vals[i] if i < len(pop_vals) else 0
                        if payout_val > 0:
                            payouts.append({
                                "race_id": race_id,
                                "bet_type": bet_type,
                                "combination": combo,
                                "payout_amount": payout_val,
                                "popularity": pop_val,
                            })
                except (ValueError, IndexError):
                    continue

        return payouts

    def _extract_combos(self, td, bet_type):
        """組み合わせTDから馬番組み合わせを抽出"""
        combos = []

        if bet_type in ("単勝", "複勝"):
            # 複勝: <div><span>5</span></div><div><span></span></div><div><span></span></div>
            # の3つのdivが1セット。空spanは無視
            divs = td.find_all("div")
            nums = []
            for d in divs:
                span = d.find("span")
                if span:
                    text = span.get_text(strip=True)
                    if text and text.isdigit():
                        nums.append(text)
            # 単勝は1つ、複勝は3つの馬番
            if bet_type == "単勝":
                combos = nums[:1]
            else:
                combos = nums
        else:
            # 馬連/ワイド/馬単/三連複/三連単: <ul><li><span>3</span></li><li><span>5</span></li><li></li></ul> で1組
            uls = td.find_all("ul")
            for ul in uls:
                lis = ul.find_all("li")
                nums = []
                for li in lis:
                    span = li.find("span")
                    if span:
                        text = span.get_text(strip=True)
                        if text and text.isdigit():
                            nums.append(text)
                if nums:
                    combos.append("-".join(nums))

        return combos

    @staticmethod
    def _extract_payout_values(td):
        """配当TDから金額リストを抽出 (br区切り対応)"""
        html = td.decode_contents()
        # <br/> で分割
        parts = re.split(r'<br\s*/?>', html, flags=re.IGNORECASE)
        vals = []
        for part in parts:
            # HTMLタグ除去 → 数字以外除去
            text = re.sub(r'<[^>]+>', '', part).strip()
            num = re.sub(r'[^\d]', '', text)
            if num:
                vals.append(int(num))
        return vals

    @staticmethod
    def _extract_popularity(td):
        """人気TDから人気順リストを抽出 (複数span対応)"""
        if not td:
            return []
        spans = td.find_all("span")
        vals = []
        for span in spans:
            text = span.get_text(strip=True)
            num = re.sub(r'[^\d]', '', text)
            if num:
                vals.append(int(num))
        return vals

    # =========================================================
    # リアルタイムオッズ取得
    # =========================================================
    def scrape_odds(self, race_id):
        """リアルタイムの単勝・複勝オッズを取得"""
        url = f"{self.BASE_URL}/odds/index.html?race_id={race_id}&type=b1"
        resp = self._get(url, encoding="EUC-JP")
        if not resp:
            return {}

        soup = BeautifulSoup(resp.text, "html.parser")
        odds_data = {}

        # 単勝オッズテーブル
        table = soup.find("table", class_="RaceOdds_HorseList_Table")
        if not table:
            # フォールバック: 出馬表からオッズを取得
            return self._scrape_odds_from_shutuba(race_id)

        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue

            try:
                horse_num = int(tds[1].get_text(strip=True))
                win_odds_text = tds[3].get_text(strip=True).replace(",", "")
                win_odds = float(win_odds_text) if win_odds_text and win_odds_text != "---" else 0
                odds_data[horse_num] = {
                    "win_odds": win_odds,
                    "place_odds_min": 0,
                    "place_odds_max": 0,
                }
            except (ValueError, IndexError):
                continue

        # 複勝オッズ
        url_place = f"{self.BASE_URL}/odds/index.html?race_id={race_id}&type=b1"
        # 複勝は同じページに含まれることが多い
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 6:
                continue
            try:
                horse_num = int(tds[1].get_text(strip=True))
                if horse_num in odds_data:
                    place_text = tds[4].get_text(strip=True).replace(",", "") if len(tds) > 4 else ""
                    if place_text and " - " in place_text:
                        parts = place_text.split(" - ")
                        odds_data[horse_num]["place_odds_min"] = float(parts[0])
                        odds_data[horse_num]["place_odds_max"] = float(parts[1])
                    elif place_text and place_text != "---":
                        try:
                            odds_data[horse_num]["place_odds_min"] = float(place_text)
                            odds_data[horse_num]["place_odds_max"] = float(place_text)
                        except ValueError:
                            pass
            except (ValueError, IndexError):
                continue

        return odds_data

    def _scrape_odds_from_shutuba(self, race_id):
        """出馬表ページからオッズを取得（フォールバック）"""
        url = f"{self.BASE_URL}/race/shutuba.html?race_id={race_id}"
        resp = self._get(url, encoding="EUC-JP")
        if not resp:
            return {}

        soup = BeautifulSoup(resp.text, "html.parser")
        odds_data = {}

        table = soup.find("table", class_="Shutuba_Table")
        if not table:
            return {}

        for tr in table.find_all("tr", class_="HorseList"):
            tds = tr.find_all("td")
            if len(tds) < 10:
                continue
            try:
                horse_num = int(tds[1].get_text(strip=True))
                # オッズは通常最後の方のカラム
                for td in reversed(tds):
                    text = td.get_text(strip=True).replace(",", "")
                    try:
                        odds_val = float(text)
                        if 1.0 < odds_val < 1000:
                            odds_data[horse_num] = {
                                "win_odds": odds_val,
                                "place_odds_min": max(odds_val * 0.25, 1.1),
                                "place_odds_max": max(odds_val * 0.4, 1.2),
                            }
                            break
                    except ValueError:
                        continue
            except (ValueError, IndexError):
                continue

        return odds_data

    # =========================================================
    # 出馬表（未来レース）の取得
    # =========================================================
    def scrape_shutuba(self, race_id):
        """出馬表をスクレイピング（当日レース予測用）"""
        url = f"{self.BASE_URL}/race/shutuba.html?race_id={race_id}"
        resp = self._get(url, encoding="EUC-JP")
        if not resp:
            return None

        soup = BeautifulSoup(resp.text, "lxml")
        race_data = {"race_id": race_id}
        race_data.update(self._parse_race_info(soup, race_id))

        entries = []
        table = soup.find("table", class_="ShutubaTable") or soup.find("table", class_="RaceTable01")
        if not table:
            print(f"⚠️ 出馬表テーブルが見つかりません: {race_id}")
            return race_data

        rows = table.find_all("tr")[1:]
        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 8:
                continue

            entry = {"race_id": race_id}
            try:
                entry["post_position"] = int(cols[0].get_text(strip=True) or 0)
                entry["horse_number"] = int(cols[1].get_text(strip=True) or 0)

                horse_tag = cols[3].find("a") if len(cols) > 3 else None
                entry["horse_name"] = cols[3].get_text(strip=True) if len(cols) > 3 else ""
                entry["horse_id"] = ""
                if horse_tag and horse_tag.get("href"):
                    h_match = re.search(r"/horse/(\w+)", horse_tag["href"])
                    if h_match:
                        entry["horse_id"] = h_match.group(1)

                sex_age = cols[4].get_text(strip=True) if len(cols) > 4 else ""
                entry["sex"] = sex_age[0] if sex_age else ""
                entry["age"] = int(sex_age[1:]) if len(sex_age) > 1 and sex_age[1:].isdigit() else 0

                impost_text = cols[5].get_text(strip=True) if len(cols) > 5 else "0"
                entry["impost"] = float(impost_text) if self._is_number(impost_text) else 0

                jockey_tag = cols[6].find("a") if len(cols) > 6 else None
                entry["jockey_name"] = cols[6].get_text(strip=True) if len(cols) > 6 else ""
                entry["jockey_id"] = ""
                if jockey_tag and jockey_tag.get("href"):
                    j_match = re.search(r"/jockey/(?:result/recent/)?(\w+)", jockey_tag["href"])
                    if j_match:
                        entry["jockey_id"] = j_match.group(1)

                trainer_tag = cols[7].find("a") if len(cols) > 7 else None
                entry["trainer_name"] = cols[7].get_text(strip=True) if len(cols) > 7 else ""
                entry["trainer_id"] = ""
                if trainer_tag and trainer_tag.get("href"):
                    t_match = re.search(r"/trainer/(?:result/recent/)?(\w+)", trainer_tag["href"])
                    if t_match:
                        entry["trainer_id"] = t_match.group(1)

                entries.append(entry)
            except (ValueError, IndexError):
                continue

        race_data["entries"] = entries
        return race_data

    # =========================================================
    # DBへの保存
    # =========================================================
    def save_race_to_db(self, race_data):
        """レースデータをDBに保存"""
        if not race_data:
            return

        with get_db() as conn:
            # レース情報保存
            conn.execute("""
                INSERT OR REPLACE INTO races
                (race_id, race_date, venue, race_number, race_name, grade,
                 distance, surface, direction, weather, track_condition, horse_count, start_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                race_data["race_id"], race_data.get("race_date", ""),
                race_data.get("venue", ""), race_data.get("race_number", 0),
                race_data.get("race_name", ""), race_data.get("grade", ""),
                race_data.get("distance", 0), race_data.get("surface", ""),
                race_data.get("direction", ""), race_data.get("weather", ""),
                race_data.get("track_condition", ""),
                len(race_data.get("results", [])),
                race_data.get("start_time", "")
            ))

            # 各馬の結果保存
            for r in race_data.get("results", []):
                # 馬マスター
                if r.get("horse_id"):
                    conn.execute("""
                        INSERT OR IGNORE INTO horses (horse_id, horse_name, sex)
                        VALUES (?, ?, ?)
                    """, (r["horse_id"], r.get("horse_name", ""), r.get("sex", "")))

                # 騎手マスター
                if r.get("jockey_id"):
                    conn.execute("""
                        INSERT OR IGNORE INTO jockeys (jockey_id, jockey_name)
                        VALUES (?, ?)
                    """, (r["jockey_id"], r.get("jockey_name", "")))

                # 調教師マスター
                if r.get("trainer_id"):
                    conn.execute("""
                        INSERT OR IGNORE INTO trainers (trainer_id, trainer_name)
                        VALUES (?, ?)
                    """, (r["trainer_id"], r.get("trainer_name", "")))

                # 結果
                conn.execute("""
                    INSERT OR REPLACE INTO results
                    (race_id, horse_id, jockey_id, trainer_id,
                     post_position, horse_number, odds, popularity,
                     finish_position, finish_time, finish_time_seconds,
                     margin, last_3f, passing_order, weight, weight_change, impost)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    race_data["race_id"], r.get("horse_id", ""),
                    r.get("jockey_id", ""), r.get("trainer_id", ""),
                    r.get("post_position", 0), r.get("horse_number", 0),
                    r.get("odds", 0), r.get("popularity", 0),
                    r.get("finish_position", 0), r.get("finish_time", ""),
                    r.get("finish_time_seconds", 0), r.get("margin", ""),
                    r.get("last_3f", 0), r.get("passing_order", ""),
                    r.get("weight", 0), r.get("weight_change", 0),
                    r.get("impost", 0)
                ))

            # 配当保存
            for p in race_data.get("payouts", []):
                conn.execute("""
                    INSERT OR REPLACE INTO payouts
                    (race_id, bet_type, combination, payout_amount, popularity)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    race_data["race_id"], p["bet_type"],
                    p["combination"], p["payout_amount"],
                    p.get("popularity", 0)
                ))

        print(f"💾 保存完了: {race_data['race_id']} ({len(race_data.get('results', []))}頭, {len(race_data.get('payouts', []))}配当)")

    # =========================================================
    # 一括収集
    # =========================================================
    def collect_month(self, year, month):
        """指定年月のレース結果を一括収集"""
        race_ids = self.get_race_list(year, month)
        print(f"🏇 {year}年{month}月: {len(race_ids)}レースを収集開始...")

        for i, race_id in enumerate(race_ids):
            print(f"  [{i+1}/{len(race_ids)}] {race_id}")
            race_data = self.scrape_race_result(race_id)
            if race_data:
                self.save_race_to_db(race_data)

        print(f"✅ {year}年{month}月の収集完了")

    def collect_range(self, start_year, start_month, end_year, end_month):
        """期間指定で一括収集"""
        y, m = start_year, start_month
        while (y, m) <= (end_year, end_month):
            self.collect_month(y, m)
            m += 1
            if m > 12:
                m = 1
                y += 1

    # =========================================================
    # ユーティリティ
    # =========================================================
    @staticmethod
    def _split_td_by_br(td):
        """<td>の中身を<br>タグで分割してリストを返す
        netkeibaの配当テーブルは複勝・ワイド等で1セルに複数値を
        <br>で区切って格納しているため、これを適切に分割する"""
        if not td:
            return []
        # decode_contents() で内部HTMLを取得し、<br>, <br/>, <br /> で分割
        html = td.decode_contents()
        parts = re.split(r'<br\s*/?>', html, flags=re.IGNORECASE)
        result = []
        for part in parts:
            # HTMLタグを除去してテキストだけ取得
            text = re.sub(r'<[^>]+>', '', part).strip()
            if text:
                result.append(text)
        return result

    @staticmethod
    def _parse_time(time_str):
        """タイム文字列を秒に変換 ('1:53.4' → 113.4)"""
        if not time_str:
            return 0
        try:
            # "1:53.4" → 113.4
            if ":" in time_str:
                parts = time_str.split(":")
                minutes = int(parts[0])
                seconds = float(parts[1])
                return minutes * 60 + seconds
            else:
                return float(time_str)
        except (ValueError, IndexError):
            return 0

    @staticmethod
    def _is_number(s):
        """文字列が数値かどうか判定"""
        try:
            float(s)
            return True
        except (ValueError, TypeError):
            return False


if __name__ == "__main__":
    scraper = NetkeibaScraper()
    print("🏇 データ収集テスト")
    print("使用法:")
    print("  scraper.collect_month(2025, 1)    # 2025年1月データを収集")
    print("  scraper.collect_range(2024, 1, 2024, 12)  # 2024年全データを収集")
