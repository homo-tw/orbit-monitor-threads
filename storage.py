import sqlite3
from datetime import datetime, timedelta


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_posts (
            url TEXT PRIMARY KEY,
            seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notified INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_places (
            place_id TEXT PRIMARY KEY,
            seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            wrote INTEGER DEFAULT 0
        )
        """
    )
    conn.commit()
    return conn


def is_seen(conn: sqlite3.Connection, url: str) -> bool:
    cur = conn.execute("SELECT 1 FROM seen_posts WHERE url = ?", (url,))
    return cur.fetchone() is not None


def mark_seen(conn: sqlite3.Connection, url: str, notified: bool = False) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO seen_posts(url, notified) VALUES (?, ?)",
        (url, 1 if notified else 0),
    )
    conn.commit()


def is_place_processed(
    conn: sqlite3.Connection, place_id: str, ttl_days: int
) -> bool:
    """這個 place_id 是不是已經處理過、可以跳過?
    - 寫過 LINE 進 sheet (wrote=1) → 永遠跳過(資料已在,不需重跑 IG+LLM)
    - 沒寫但 seen_at 還在 TTL 內 → 跳過(避免短期內重試)
    - 沒寫且過 TTL → 不跳過,讓 caller 重新處理(可能店家後來補了 LINE)"""
    cur = conn.execute(
        "SELECT wrote, seen_at FROM seen_places WHERE place_id = ?",
        (place_id,),
    )
    row = cur.fetchone()
    if not row:
        return False
    wrote, seen_at = row
    if wrote:
        return True
    if not seen_at:
        return False
    try:
        seen_dt = datetime.fromisoformat(seen_at)
    except (TypeError, ValueError):
        return False
    return seen_dt > datetime.now() - timedelta(days=ttl_days)


def mark_place_processed(
    conn: sqlite3.Connection, place_id: str, wrote: bool
) -> None:
    """UPSERT seen_places。wrote=True 蓋掉之前的 False,wrote=False 不會洗掉之前的 True。"""
    conn.execute(
        """
        INSERT INTO seen_places(place_id, wrote) VALUES (?, ?)
        ON CONFLICT(place_id) DO UPDATE SET
            wrote = MAX(wrote, excluded.wrote),
            seen_at = CURRENT_TIMESTAMP
        """,
        (place_id, 1 if wrote else 0),
    )
    conn.commit()
