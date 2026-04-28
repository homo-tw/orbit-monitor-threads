import os
import re
from urllib.parse import unquote

import gspread
import requests

from config import GOOGLE_CREDENTIALS_FILE, SPREADSHEET_ID, SHEET_NAME

LIN_EE_PATTERN = re.compile(r"https?://lin\.ee/[A-Za-z0-9_\-]+", re.IGNORECASE)
LINE_ME_PATTERN = re.compile(
    r"https?://line\.me/(?:R/)?ti/p/@?[A-Za-z0-9_\-\.]+", re.IGNORECASE
)
LIN_EE_NO_PROTO = re.compile(r"\blin\.ee/[A-Za-z0-9_\-]+", re.IGNORECASE)

_LINE_TI_ID_RE = re.compile(
    r"line\.me/(?:R/)?ti/p/(?:@|%40)?([A-Za-z0-9_\-\.]+)", re.IGNORECASE
)
_PAGE_LINE_ID_RE = re.compile(
    r"page\.line\.me/(?:@|%40)?([A-Za-z0-9_\-\.]+)", re.IGNORECASE
)

_RESOLVE_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_resolve_cache: dict[str, str] = {}


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


def _extract_line_id(url: str) -> str:
    decoded = unquote(url or "")
    m = _LINE_TI_ID_RE.search(decoded) or _PAGE_LINE_ID_RE.search(decoded)
    if not m:
        return ""
    return m.group(1).lstrip("@").lower()


def resolve_line_id_url(line_url: str) -> str:
    """把 lin.ee 短網址展開成正規化的 https://line.me/R/ti/p/@xxxxx。
    已經是 line.me / page.line.me 形式的也一併標準化。
    展不開(LIFF / 群組邀請 / 失效)回空字串。"""
    if not line_url:
        return ""

    direct = _extract_line_id(line_url)
    if direct:
        return f"https://line.me/R/ti/p/@{direct}"

    if line_url in _resolve_cache:
        return _resolve_cache[line_url]

    try:
        r = requests.get(
            line_url,
            allow_redirects=True,
            timeout=10,
            headers={"User-Agent": _RESOLVE_UA},
        )
    except requests.RequestException as e:
        print(f"[LINE] resolve {line_url} 失敗: {e}", flush=True)
        _resolve_cache[line_url] = ""
        return ""

    final_id = _extract_line_id(r.url)
    resolved = f"https://line.me/R/ti/p/@{final_id}" if final_id else ""
    _resolve_cache[line_url] = resolved
    return resolved


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
