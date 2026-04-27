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
    """讀 sheet 拿到已收集的 Threads 帳號,key=username(小寫)。
    格式對齊 orbit-spider/ig_playwright.py:load_cache。"""
    if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
        print(f"[LINE] 找不到 {GOOGLE_CREDENTIALS_FILE},以空 cache 啟動", flush=True)
        return {}
    try:
        ws = _get_worksheet()
        col_d = ws.col_values(4)
        col_e = ws.col_values(5)
        cache = {}
        for i, url in enumerate(col_e):
            if not url or "threads.net/" not in url:
                continue
            username = username_from_url(url)
            if not username:
                continue
            cache[username] = {
                "username": username,
                "url": url,
                "bio": col_d[i] if i < len(col_d) else "",
            }
        print(f"[LINE] 從 sheet 載入 {len(cache)} 個 Threads 帳號 cache", flush=True)
        return cache
    except Exception as e:
        print(f"[LINE] 載入 cache 失敗: {e}", flush=True)
        return {}


def save_account(
    username: str, bio: str, profile_url: str, line_url: str, cache: dict
) -> bool:
    """寫一筆帳號到 sheet。回傳 True=新寫入,False=已存在。
    寫入前再讀一次 sheet E 欄 double-check,防止跨 process 併發重複寫。"""
    key = username.lower()
    if key in cache:
        return False

    ws = _get_worksheet()
    existing_usernames = {username_from_url(u) for u in ws.col_values(5) if u}
    if key in existing_usernames:
        cache[key] = {"username": key, "url": profile_url, "bio": bio}
        return False

    col_d = ws.col_values(4)
    row = len(col_d) + 1
    ws.update(f"D{row}:E{row}", [[bio, profile_url]])
    ws.update(f"L{row}:L{row}", [[line_url]])

    cache[key] = {"username": key, "url": profile_url, "bio": bio}
    return True
