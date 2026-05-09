"""
競馬データベース管理モジュール
SQLiteでレース・馬・騎手・調教師・結果データを管理
"""

import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "keiba.db")


def get_connection(db_path=None):
    """DB接続を取得"""
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db(db_path=None):
    """コンテキストマネージャでDB接続を管理"""
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path=None):
    """データベースの初期化（テーブル作成）"""
    with get_db(db_path) as conn:
        conn.executescript("""
            -- レース情報
            CREATE TABLE IF NOT EXISTS races (
                race_id TEXT PRIMARY KEY,          -- 例: 202505030811
                race_date TEXT NOT NULL,            -- 開催日 YYYY-MM-DD
                venue TEXT NOT NULL,                -- 競馬場名（東京, 中山, 阪神 等）
                race_number INTEGER NOT NULL,       -- レース番号
                race_name TEXT,                     -- レース名
                grade TEXT,                         -- グレード（G1, G2, G3, OP, 条件 等）
                distance INTEGER NOT NULL,          -- 距離(m)
                surface TEXT NOT NULL,              -- 芝/ダート/障害
                direction TEXT,                     -- 右/左
                weather TEXT,                       -- 天候
                track_condition TEXT,               -- 馬場状態（良/稍重/重/不良）
                horse_count INTEGER,                -- 出走頭数
                start_time TEXT,                    -- 発走時刻 (HH:MM)
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 馬マスター
            CREATE TABLE IF NOT EXISTS horses (
                horse_id TEXT PRIMARY KEY,          -- netkeiba上の馬ID
                horse_name TEXT NOT NULL,           -- 馬名
                sex TEXT,                           -- 性別（牡/牝/セ）
                birth_year INTEGER,                -- 生年
                sire TEXT,                          -- 父（種牡馬）
                dam TEXT,                           -- 母
                damsire TEXT,                       -- 母父
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 騎手マスター
            CREATE TABLE IF NOT EXISTS jockeys (
                jockey_id TEXT PRIMARY KEY,
                jockey_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 調教師マスター
            CREATE TABLE IF NOT EXISTS trainers (
                trainer_id TEXT PRIMARY KEY,
                trainer_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- レース結果（出走馬ごとのデータ）
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                race_id TEXT NOT NULL,
                horse_id TEXT NOT NULL,
                jockey_id TEXT,
                trainer_id TEXT,
                post_position INTEGER,             -- 枠番
                horse_number INTEGER,               -- 馬番
                odds REAL,                          -- 単勝オッズ
                popularity INTEGER,                 -- 人気
                finish_position INTEGER,            -- 着順 (0=除外/取消)
                finish_time TEXT,                   -- 走破タイム (例: "1:34.5")
                finish_time_seconds REAL,           -- 走破タイム(秒)
                margin TEXT,                        -- 着差
                last_3f REAL,                       -- 上がり3F(秒)
                passing_order TEXT,                 -- 通過順 (例: "3-3-2-1")
                weight INTEGER,                    -- 馬体重(kg)
                weight_change INTEGER,              -- 馬体重増減
                impost REAL,                        -- 斤量
                corner_positions TEXT,              -- コーナー通過順位
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (race_id) REFERENCES races(race_id),
                FOREIGN KEY (horse_id) REFERENCES horses(horse_id),
                UNIQUE(race_id, horse_number)
            );

            -- 配当テーブル
            CREATE TABLE IF NOT EXISTS payouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                race_id TEXT NOT NULL,
                bet_type TEXT NOT NULL,             -- 単勝/複勝/枠連/馬連/ワイド/馬単/三連複/三連単
                combination TEXT NOT NULL,          -- 組み合わせ (例: "3", "3-5", "3-5-8")
                payout_amount INTEGER NOT NULL,     -- 払戻金(円)
                popularity INTEGER DEFAULT 0,       -- 人気
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (race_id) REFERENCES races(race_id),
                UNIQUE(race_id, bet_type, combination)
            );

            CREATE INDEX IF NOT EXISTS idx_payouts_race ON payouts(race_id);

            -- 追い切り評価テーブル(netkeiba oikiri.html)
            CREATE TABLE IF NOT EXISTS workouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                race_id TEXT NOT NULL,
                horse_id TEXT NOT NULL,
                horse_number INTEGER,
                evaluation_grade TEXT,         -- A/B/C
                evaluation_text TEXT,          -- "気力充実" 等の自然言語ラベル
                comment TEXT,                  -- 詳細コメント(あれば)
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (race_id) REFERENCES races(race_id),
                UNIQUE(race_id, horse_id)
            );

            CREATE INDEX IF NOT EXISTS idx_workouts_race ON workouts(race_id);
            CREATE INDEX IF NOT EXISTS idx_workouts_horse ON workouts(horse_id);

            -- 予測キャッシュテーブル（予想固定化用）
            CREATE TABLE IF NOT EXISTS predictions_cache (
                race_id TEXT PRIMARY KEY,
                predictions_json TEXT NOT NULL,    -- 全馬の予測結果（horses配列）
                all_bets_json TEXT NOT NULL,       -- 全6券種の買い目
                confidence TEXT NOT NULL,           -- S/A/B/C/D
                conf_reason TEXT,                   -- 信頼度理由
                should_bet INTEGER DEFAULT 1,
                bet_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                posted_at TIMESTAMP NULL            -- post_predict 投稿時刻(これが入ったら以降変更不可)
            );

            -- 収支管理テーブル
            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                race_id TEXT NOT NULL,
                bet_type TEXT NOT NULL,             -- 単勝/複勝/馬連/ワイド/三連複/三連単
                bet_detail TEXT NOT NULL,           -- 馬番の組み合わせ
                amount INTEGER NOT NULL,            -- 賭け金(円)
                odds REAL,                          -- オッズ
                is_hit INTEGER DEFAULT 0,           -- 的中したか (0/1)
                payout INTEGER DEFAULT 0,           -- 払戻金(円)
                predicted_prob REAL,                -- モデル予測確率
                expected_value REAL,                -- 期待値
                bet_date TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (race_id) REFERENCES races(race_id)
            );

            -- インデックス作成
            CREATE INDEX IF NOT EXISTS idx_races_date ON races(race_date);
            CREATE INDEX IF NOT EXISTS idx_races_venue ON races(venue);
            CREATE INDEX IF NOT EXISTS idx_results_race ON results(race_id);
            CREATE INDEX IF NOT EXISTS idx_results_horse ON results(horse_id);
            CREATE INDEX IF NOT EXISTS idx_results_jockey ON results(jockey_id);
            CREATE INDEX IF NOT EXISTS idx_bets_race ON bets(race_id);
            CREATE INDEX IF NOT EXISTS idx_bets_date ON bets(bet_date);
        """)

        # ── マイグレーション: predictions_cache に posted_at がなければ追加 ──
        cols = [r[1] for r in conn.execute("PRAGMA table_info(predictions_cache)").fetchall()]
        if 'posted_at' not in cols:
            conn.execute("ALTER TABLE predictions_cache ADD COLUMN posted_at TIMESTAMP NULL")
            print("🔧 predictions_cache: posted_at カラム追加")
    print("✅ データベース初期化完了")


def seal_predictions_for_date(date_str):
    """指定日の predictions_cache を「投稿済み」としてロック。
    post_predict 投稿成功時に呼ぶ。
    """
    if len(date_str) == 8:
        hy = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    else:
        hy = date_str
    with get_db() as conn:
        n = conn.execute("""
            UPDATE predictions_cache
            SET posted_at = CURRENT_TIMESTAMP
            WHERE race_id IN (
                SELECT race_id FROM races WHERE race_date = ? OR race_date = ?
            ) AND posted_at IS NULL
        """, (date_str, hy)).rowcount
    print(f"🔒 predictions_cache を {n} レース seal({date_str})")
    return n


def is_prediction_sealed(race_id):
    """そのレースの予測が投稿済み(seal)かを返す"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT posted_at FROM predictions_cache WHERE race_id = ?",
            (race_id,)
        ).fetchone()
        return row is not None and row['posted_at'] is not None


def sync_cache_race_ids(race_date_str):
    """predictions_cacheのrace_idをracesテーブルの最新IDに同期する。

    netkeibaのrace_idの日目(day)部分が朝と昼で変わる問題を解決。
    race_id形式: YYYY(4) + venue(2) + kai(2) + day(2) + race(2)

    例: 朝に 202606030101 で予測保存 → 昼に races が 202606030501 に更新
        → cache の race_id を 202606030501 にリネーム
    """
    # race_dateを YYYY-MM-DD 形式に統一
    if len(race_date_str) == 8:
        date_hyphen = f"{race_date_str[:4]}-{race_date_str[4:6]}-{race_date_str[6:8]}"
    else:
        date_hyphen = race_date_str

    renamed = 0
    with get_db() as conn:
        # 当日のracesを取得
        races = conn.execute("""
            SELECT race_id, venue, race_number FROM races
            WHERE race_date = ? OR race_date = ?
        """, (race_date_str, date_hyphen)).fetchall()

        for race in races:
            rid = race['race_id']
            venue_code = rid[4:6]
            kai_code = rid[6:8]
            race_num = rid[10:12]

            # 既にcacheにある → スキップ
            exists = conn.execute(
                "SELECT 1 FROM predictions_cache WHERE race_id = ?", (rid,)
            ).fetchone()
            if exists:
                continue

            # 同じ会場+開催回+レース番号で、当日作成されたcacheを検索
            old = conn.execute("""
                SELECT race_id FROM predictions_cache
                WHERE substr(race_id,5,2) = ?
                  AND substr(race_id,7,2) = ?
                  AND substr(race_id,11,2) = ?
                  AND date(created_at) = ?
                ORDER BY created_at DESC LIMIT 1
            """, (venue_code, kai_code, race_num, date_hyphen)).fetchone()

            if old and old['race_id'] != rid:
                old_id = old['race_id']
                conn.execute(
                    "UPDATE predictions_cache SET race_id = ? WHERE race_id = ?",
                    (rid, old_id)
                )
                renamed += 1

    if renamed > 0:
        print(f"🔄 predictions_cache: {renamed}件のrace_idを同期しました")


if __name__ == "__main__":
    init_db()
