import os
import re

import gspread

from config import GOOGLE_CREDENTIALS_FILE, SPREADSHEET_ID, SHEET_NAME

LIN_EE_PATTERN = re.compile(r"https?://lin\.ee/[A-Za-z0-9_\-]+", re.IGNORECASE)
LINE_ME_PATTERN = re.compile(
    r"https?://line\.me/(?:R/)?ti/p/@?[A-Za-z0-9_\-\.]+", re.IGNORECASE
)
LIN_EE_NO_PROTO = re.compile(r"\blin\.ee/[A-Za-z0-9_\-]+", re.IGNORECASE)


def extract_line_url(text: str) -> str:
    if not text:
        return ""
    for pat in (LIN_EE_PATTERN, LINE_ME_PATTERN):
        m = pat.search(text)
        if m:
            return m.group(0)
    m = LIN_EE_NO_PROTO.search(text)
    if m:
        return f"https://{m.group(0)}"
    return ""


def username_from_url(url: str) -> str:
    if not url:
        return ""
    tail = url.strip().rstrip("/").split("/")[-1]
    return tail.lstrip("@").lower()


def line_key(line_url: str) -> str:
    return line_url.strip().rstrip("/").lower() if line_url else ""


_worksheet = None


def _get_worksheet():
    global _worksheet
    if _worksheet is not None:
        return _worksheet
    if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
        raise FileNotFoundError(GOOGLE_CREDENTIALS_FILE)
    gc = gspread.service_account(filename=GOOGLE_CREDENTIALS_FILE)
    sh = gc.open_by_key(SPREADSHEET_ID)
    _worksheet = sh.worksheet(SHEET_NAME)
    return _worksheet


def load_cache() -> dict:
    """讀 sheet 拿到已收集帳號,key 同時包含:
    - Threads username(小寫,只看 E 欄含 threads.net 的列)
    - LINE URL(整列 L 欄都納入,含 orbit-spider 寫的 IG lin.ee)
    任一命中就視為已存在。"""
    if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
        print(f"[LINE] 找不到 {GOOGLE_CREDENTIALS_FILE},以空 cache 啟動", flush=True)
        return {}
    try:
        ws = _get_worksheet()
        col_d = ws.col_values(4)
        col_e = ws.col_values(5)
        col_l = ws.col_values(12)
        cache: dict = {}
        for i, url in enumerate(col_e):
            if not url or "threads.net/" not in url:
                continue
            username = username_from_url(url)
            if username:
                cache[username] = {
                    "username": username,
                    "url": url,
                    "bio": col_d[i] if i < len(col_d) else "",
                }
        threads_count = len(cache)
        for url in col_l:
            k = line_key(url)
            if k:
                cache.setdefault(k, {"line_url": url})
        line_count = len(cache) - threads_count
        print(
            f"[LINE] cache 載入: Threads {threads_count} 個, LINE URL {line_count} 個",
            flush=True,
        )
        return cache
    except Exception as e:
        print(f"[LINE] 載入 cache 失敗: {e}", flush=True)
        return {}


def save_account(
    username: str, bio: str, profile_url: str, line_url: str, cache: dict
) -> bool:
    """寫一筆帳號到 sheet。回傳 True=新寫入,False=已存在。
    寫入前再讀一次 sheet E/L 欄 double-check,防止跨 process 併發重複寫。"""
    user_key = username.lower()
    l_key = line_key(line_url)
    if user_key in cache or (l_key and l_key in cache):
        return False

    ws = _get_worksheet()
    existing_users = {username_from_url(u) for u in ws.col_values(5) if u}
    existing_lines = {line_key(u) for u in ws.col_values(12) if u}
    if user_key in existing_users or (l_key and l_key in existing_lines):
        cache[user_key] = {"username": user_key, "url": profile_url, "bio": bio}
        if l_key:
            cache[l_key] = {"line_url": line_url}
        return False

    col_d = ws.col_values(4)
    row = len(col_d) + 1
    ws.update(f"D{row}:E{row}", [[bio, profile_url]])
    ws.update(f"L{row}:L{row}", [[line_url]])

    cache[user_key] = {"username": user_key, "url": profile_url, "bio": bio}
    if l_key:
        cache[l_key] = {"line_url": line_url}
    return True
