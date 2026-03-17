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

            -- 予測キャッシュテーブル（予想固定化用）
            CREATE TABLE IF NOT EXISTS predictions_cache (
                race_id TEXT PRIMARY KEY,
                predictions_json TEXT NOT NULL,    -- 全馬の予測結果（horses配列）
                all_bets_json TEXT NOT NULL,       -- 全6券種の買い目
                confidence TEXT NOT NULL,           -- S/A/B/C/D
                conf_reason TEXT,                   -- 信頼度理由
                should_bet INTEGER DEFAULT 1,
                bet_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    print("✅ データベース初期化完了")


if __name__ == "__main__":
    init_db()
