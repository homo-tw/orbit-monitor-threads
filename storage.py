import sqlite3


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
