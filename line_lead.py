import os
import re
import unicodedata
from urllib.parse import unquote

import gspread
import requests
from openai import AsyncOpenAI

from config import (
    GOOGLE_CREDENTIALS_FILE,
    OPENAI_API_KEY,
    OPENAI_MODEL,
    SHEET_NAME,
    SPREADSHEET_ID,
)

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


_BIO_LINE_KEYWORD_RE = re.compile(
    # 大寫獨立字 LINE (排除 "ONLINE" / "DEADLINE")
    r"(?<![A-Za-z])LINE(?![A-Za-z])"
    # line 後接明確標記 (前必須非字母,排掉 "online:" / "deadline:")
    r"|(?<![A-Za-z])[Ll]ine\s*(?:@|＠|:|：|ID|id|帳號)"
    # CJK 上下文裡的 Line — 兩側都要避開字母,免得 "deadline 是" / "中文Linehead" 誤判
    r"|(?<=[一-鿿])\s*[Ll]ine(?![A-Za-z])"
    r"|(?<![A-Za-z])[Ll]ine\s*(?=[一-鿿])"
    # 其他寫法
    r"|@line|＠line|賴|ライン|라인",
)


def bio_mentions_line(bio: str) -> bool:
    """bio 文字裡有沒有 LINE 字眼。沒提 LINE 的 bio 別丟給 LLM,避免幻覺。"""
    return bool(bio) and bool(_BIO_LINE_KEYWORD_RE.search(bio))


def normalize_handle(s: str) -> str:
    """LLM 可能回 ＠𝟪𝟢𝟨𝗉𝗄 之類花體變體 — NFKC 後拿來跟 username 比對。
    僅供比對用,不要把這個正規化過的值寫進 sheet。"""
    if not s:
        return ""
    return unicodedata.normalize("NFKC", s).strip().lstrip("@＠").strip().lower()


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
    """把 lin.ee 短網址展開成正規化的 @xxxxx LINE ID。
    已經是 line.me / page.line.me 形式的也一併萃成 @ID。
    展不開(LIFF / 群組邀請 / 失效)回空字串。"""
    if not line_url:
        return ""

    direct = _extract_line_id(line_url)
    if direct:
        return f"@{direct}"

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
    resolved = f"@{final_id}" if final_id else ""
    _resolve_cache[line_url] = resolved
    return resolved


def username_from_url(url: str) -> str:
    if not url:
        return ""
    tail = url.strip().rstrip("/").split("/")[-1]
    return tail.lstrip("@").lower()


def line_key(line_url: str) -> str:
    if not line_url:
        return ""
    extracted = _extract_line_id(line_url)
    if extracted:
        return f"@{extracted}"
    return line_url.strip().rstrip("/").lower()


_llm_client: AsyncOpenAI | None = None


def _get_llm_client() -> AsyncOpenAI:
    global _llm_client
    if _llm_client is None:
        _llm_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _llm_client


_LLM_PROMPT = """從以下 Threads 個人介紹文字中,找出 LINE 聯絡方式並回傳。

可能的形式:
- LINE 短網址,例如 lin.ee/abc123 或 line.me/ti/p/@abc123
- LINE 帳號 ID,前面通常有 'LINE:' / '賴:' / 'Line@' 等字,例如 @abc123

注意事項:
- 帳號 ID 可能用花體字 / 全形 / 數學符號等變體寫,例如 ＠𝟪𝟢𝟨𝗉𝗄𝗐𝗒𝗇 — 請**完整保留原始字形**回傳,不要做任何正規化或字元轉換
- 短網址直接回傳完整 URL(若含 https:// 一併回傳;無 https 也照抄)
- 找不到回傳空字串
- 不要有任何解釋或多餘文字,只回傳那一個值

文字:
{bio}

回傳:"""


async def extract_line_via_llm(bio: str) -> str:
    """regex 抓不到 LINE URL 時,丟 bio 給 LLM 抽 LINE 識別。
    回傳:
    - 'lin.ee/...' / 'line.me/...' / 'page.line.me/...' 短網址(交給 resolve_line_id_url 展開)
    - '@xxx' 形式的 LINE @ID(保留原始 unicode 字形,直接寫 sheet)
    - 找不到回傳空字串
    """
    if not bio or not bio.strip():
        return ""
    try:
        resp = await _get_llm_client().chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": _LLM_PROMPT.format(bio=bio[:1500])}],
            max_tokens=80,
            temperature=0,
        )
    except Exception as e:
        print(f"[LINE] LLM 抽取失敗: {e}", flush=True)
        return ""
    return (resp.choices[0].message.content or "").strip()


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
    - Threads username(小寫,只看 F 欄含 threads.net/threads.com 的列)
    - LINE URL(整列 M 欄都納入,含 orbit-spider 寫的 IG lin.ee)
    任一命中就視為已存在。"""
    if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
        print(f"[LINE] 找不到 {GOOGLE_CREDENTIALS_FILE},以空 cache 啟動", flush=True)
        return {}
    try:
        ws = _get_worksheet()
        col_e = ws.col_values(5)
        col_f = ws.col_values(6)
        col_m = ws.col_values(13)
        cache: dict = {}
        for i, url in enumerate(col_f):
            if not url:
                continue
            # Threads / IG profile URL 都拿來當 dedupe key,避免 Places 帶 IG 跟 Threads 撞號重做白工
            if not any(
                host in url
                for host in ("threads.net/", "threads.com/", "instagram.com/")
            ):
                continue
            username = username_from_url(url)
            if username:
                cache[username] = {
                    "username": username,
                    "url": url,
                    "bio": col_e[i] if i < len(col_e) else "",
                }
        threads_count = len(cache)
        for url in col_m:
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
    username: str,
    bio: str,
    profile_url: str,
    line_url: str,
    threads_follower_count: str,
    ig_follower_count: str,
    cache: dict,
    source: str = "threads",
) -> bool:
    """寫一筆帳號到 sheet。回傳 True=新寫入,False=已存在。
    寫入前再讀一次 sheet F/M 欄 double-check,防止跨 process 併發重複寫。
    N 欄 = Threads follower 數, O 欄 = IG follower 數, P 欄 = 來源 tag。"""
    user_key = username.lower()
    l_key = line_key(line_url)
    if user_key in cache or (l_key and l_key in cache):
        return False

    ws = _get_worksheet()
    existing_users = {username_from_url(u) for u in ws.col_values(6) if u}
    existing_lines = {line_key(u) for u in ws.col_values(13) if u}
    if user_key in existing_users or (l_key and l_key in existing_lines):
        cache[user_key] = {"username": user_key, "url": profile_url, "bio": bio}
        if l_key:
            cache[l_key] = {"line_url": line_url}
        return False

    col_e = ws.col_values(5)
    row = len(col_e) + 1
    ws.update(f"E{row}:F{row}", [[bio, profile_url]])
    ws.update(
        f"M{row}:P{row}",
        [[line_url, threads_follower_count, ig_follower_count, source]],
    )

    cache[user_key] = {"username": user_key, "url": profile_url, "bio": bio}
    if l_key:
        cache[l_key] = {"line_url": line_url}
    return True
