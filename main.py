import asyncio
import os
import re
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.async_api import async_playwright

from config import (
    KEYWORDS,
    LINE_LEAD_KEYWORDS,
    LINE_LEAD_MAX_AGE_DAYS,
    LINE_LEAD_SCROLLS,
    SCAN_INTERVAL_SECONDS,
    MAX_AGE_DAYS,
    MATCH_CONFIDENCE_THRESHOLD,
    STORAGE_STATE_PATH,
    DB_PATH,
    get_proxy_config,
)
from storage import init_db, is_seen, mark_seen
from scraper import (
    SessionExpiredError,
    fetch_threads_profile,
    scrape_keyword,
    scrape_post_replies,
)
from classifier import classify
from notifier import notify_batch, notify_alert
from line_lead import (
    extract_line_url,
    extract_line_via_llm,
    load_cache,
    resolve_line_id_url,
    save_account,
)

ALERT_MARKER = ".session_alert_sent"
ALERT_THROTTLE_HOURS = 6
CRON_LOG = Path(__file__).parent / "cron.log"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def log_startup(label: str) -> None:
    """每個 cron 進入點啟動時 append 一行到 cron.log,確認排程有觸發。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(CRON_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] started ({label})\n")
    except Exception:
        pass


@asynccontextmanager
async def open_browser_session():
    """共用 browser setup:proxy / storage state / 單一 page。yield (conn, page)。"""
    conn = init_db(DB_PATH)
    proxy = get_proxy_config()
    async with async_playwright() as p:
        launch_kwargs = {"headless": True}
        if proxy:
            launch_kwargs["proxy"] = proxy
            log(f"使用 proxy: {proxy['server']}")
        browser = await p.chromium.launch(**launch_kwargs)
        try:
            ctx_kwargs = {}
            if os.path.exists(STORAGE_STATE_PATH):
                ctx_kwargs["storage_state"] = STORAGE_STATE_PATH
                log(f"使用 {STORAGE_STATE_PATH} 登入 session")
            else:
                log("沒有 storage_state.json,以匿名瀏覽(部分內容可能受限)")
            context = await browser.new_context(**ctx_kwargs)
            page = await context.new_page()
            yield conn, page
        finally:
            await browser.close()


def _should_alert_session() -> bool:
    if not os.path.exists(ALERT_MARKER):
        return True
    age = time.time() - os.path.getmtime(ALERT_MARKER)
    return age > ALERT_THROTTLE_HOURS * 3600


def _mark_session_alerted() -> None:
    open(ALERT_MARKER, "w").close()


def _clear_session_alert() -> None:
    if os.path.exists(ALERT_MARKER):
        os.remove(ALERT_MARKER)


def _is_recent(post: dict, cutoff: datetime) -> bool:
    ts = post.get("published")
    if not ts:
        return False  # 沒時間戳就跳過,避免撈到太舊的
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    return dt >= cutoff


async def run_once(conn, page) -> None:
    matches: list[tuple[dict, dict]] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

    for kw in KEYWORDS:
        log(f"搜尋關鍵字: {kw}")
        try:
            posts = await scrape_keyword(page, kw)
        except SessionExpiredError as e:
            log(f"⚠️ session 過期: {e}")
            if _should_alert_session():
                notify_alert(
                    "⚠️ Threads session 已過期,本機重跑 `python login.py` "
                    "後把新的 `storage_state.json` 上傳到 server"
                )
                _mark_session_alerted()
            return
        except Exception as e:
            log(f"  scrape 失敗: {e}")
            continue
        log(f"  抓到 {len(posts)} 則")

        recent = [p for p in posts if _is_recent(p, cutoff)]
        if len(recent) < len(posts):
            log(f"  過濾後 {len(recent)} 則在近 {MAX_AGE_DAYS} 天內")

        for post in recent:
            if is_seen(conn, post["url"]):
                continue
            try:
                result = classify(post["text"])
            except Exception as e:
                log(f"  classify 失敗: {e}")
                continue

            if result["match"] and result["confidence"] >= MATCH_CONFIDENCE_THRESHOLD:
                matches.append((post, result))
                log(
                    f"  ✅ match @{post['author']} "
                    f"(conf={result['confidence']:.2f}) {post['url']}"
                )
            else:
                if result["match"]:
                    log(
                        f"  ⚠️ match 但 confidence {result['confidence']:.2f} "
                        f"< {MATCH_CONFIDENCE_THRESHOLD},略過: {post['url']}"
                    )
                mark_seen(conn, post["url"], notified=False)

    # 能順利跑完所有關鍵字代表 session 還活著,清掉警示節流
    _clear_session_alert()

    if not matches:
        log("本輪無 match")
        return

    results = notify_batch(matches)
    for (post, _), ok in zip(matches, results):
        mark_seen(conn, post["url"], notified=ok)
    log(f"通知 {sum(results)}/{len(results)} 則")


# 接預約訊號:中文直接 substring,英文用 word boundary(避免誤命中 admin/random)
_BOOKING_CJK = ("預約", "預訂", "私訊")
_BOOKING_EN_RE = re.compile(r"\b(dm|book|booking)\b", re.IGNORECASE)


def _has_booking_signal(text: str) -> bool:
    if not text:
        return False
    if any(k in text for k in _BOOKING_CJK):
        return True
    return bool(_BOOKING_EN_RE.search(text))


def _is_hk_username(username: str) -> bool:
    """username 結尾 hk 或含 _hk / .hk / hk_ / hk. 視為香港帳號。"""
    u = username.lower()
    return (
        u.endswith("hk")
        or "_hk" in u
        or ".hk" in u
        or "hk_" in u
        or "hk." in u
    )


async def _process_author_candidate(
    page,
    cache: dict,
    checked_authors: set,
    author: str,
    primary_text: str,
    extra_booking_context: str = "",
    source_prefix: str = "",
) -> bool:
    """處理一個 candidate author:fetch profile → 預約 filter → LINE extract → 寫 sheet。
    回傳 True 表示有寫入新一筆,False 表示略過/已存在/失敗。
    SessionExpiredError 會直接 raise 給 caller 處理。"""
    author_key = author.lower()
    if author_key in cache or author_key in checked_authors:
        return False
    checked_authors.add(author_key)

    if _is_hk_username(author):
        log(f"[LINE]   @{author} HK 帳號,略過")
        return False

    try:
        bio, search_blob = await fetch_threads_profile(page, author)
    except SessionExpiredError:
        raise
    except Exception as e:
        log(f"[LINE]   fetch_profile @{author} 失敗: {e}")
        bio, search_blob = "", ""

    # 接預約訊號 filter:primary_text(post/reply 文)、extra(reply 場景下傳入 OP 文)、bio
    # 至少一個含「預約 / 預訂 / 私訊 / DM / book / booking」才算服務商家
    if not (
        _has_booking_signal(primary_text)
        or _has_booking_signal(extra_booking_context)
        or _has_booking_signal(bio)
    ):
        log(f"[LINE]   @{author} 無預約/預訂/私訊/DM/book 訊號,略過")
        return False

    raw_line_url = extract_line_url(search_blob)
    source = f"{source_prefix}profile-url"

    if not raw_line_url:
        raw_line_url = extract_line_url(primary_text)
        if raw_line_url:
            source = f"{source_prefix}post-url"

    line_url = ""
    if raw_line_url:
        line_url = resolve_line_id_url(raw_line_url)
        if not line_url:
            log(f"[LINE]   @{author} {raw_line_url} 無法展開成 @ID,略過")
            return False
    else:
        llm_result = await extract_line_via_llm(bio)
        if not llm_result:
            log(f"[LINE]   @{author} 無 LINE(regex/LLM 都沒),略過")
            return False
        if "lin.ee" in llm_result.lower() or "line.me" in llm_result.lower():
            line_url = resolve_line_id_url(llm_result)
            if not line_url:
                log(f"[LINE]   @{author} LLM 給 {llm_result} 但無法展開,略過")
                return False
            source = f"{source_prefix}llm-url"
        else:
            # @xxx 文字版 — 保留原始字形,直接當 L 欄值
            line_url = llm_result
            source = f"{source_prefix}llm-text"

    profile_url = f"https://www.threads.com/@{author}"
    try:
        wrote = save_account(author_key, bio[:500], profile_url, line_url, cache)
        if wrote:
            log(f"[LINE]   ✅ 寫入 @{author} {line_url} ({source})")
        else:
            log(f"[LINE]   ⏭️ @{author} 或 {line_url} 已存在 sheet")
        return wrote
    except Exception as e:
        log(f"[LINE]   寫 sheet 失敗 @{author}: {e}")
        return False


async def run_line_lead_once(conn, page) -> None:
    if not LINE_LEAD_KEYWORDS:
        return

    cache = load_cache()
    cutoff = datetime.now(timezone.utc) - timedelta(days=LINE_LEAD_MAX_AGE_DAYS)
    checked_authors: set[str] = set()  # 同一輪內同 author 只抓一次 profile
    new_count = 0

    for kw in LINE_LEAD_KEYWORDS:
        log(f"[LINE] 搜尋: {kw}")
        try:
            posts = await scrape_keyword(page, kw, scrolls=LINE_LEAD_SCROLLS)
        except SessionExpiredError as e:
            log(f"[LINE] ⚠️ session 過期: {e}")
            return
        except Exception as e:
            log(f"[LINE]   scrape 失敗: {e}")
            continue

        recent = [p for p in posts if _is_recent(p, cutoff)]
        log(f"[LINE]   抓到 {len(posts)} 則,近 {LINE_LEAD_MAX_AGE_DAYS} 天內 {len(recent)} 則")

        for post in recent:
            if is_seen(conn, post["url"]):
                continue
            op_post_text = post.get("text") or ""

            # 1. 處理 OP author(可能就是商家自介)
            try:
                op_wrote = await _process_author_candidate(
                    page, cache, checked_authors,
                    author=post["author"],
                    primary_text=op_post_text,
                )
            except SessionExpiredError as e:
                log(f"[LINE] ⚠️ session 過期(OP profile): {e}")
                return
            if op_wrote:
                new_count += 1

            # 2. 進詳情頁抓 replies(消費者求推薦時,商家會在 reply 自我推銷)
            try:
                replies = await scrape_post_replies(page, post["url"], scrolls=5)
            except SessionExpiredError as e:
                log(f"[LINE] ⚠️ session 過期(replies): {e}")
                return
            except Exception as e:
                log(f"[LINE]   scrape_replies 失敗 {post['url']}: {e}")
                replies = []

            if replies:
                log(f"[LINE]   {post['url']} 抓到 {len(replies)} 個 reply")

            for reply in replies:
                try:
                    r_wrote = await _process_author_candidate(
                        page, cache, checked_authors,
                        author=reply["author"],
                        primary_text=reply.get("text") or "",
                        extra_booking_context=op_post_text,
                        source_prefix="reply-",
                    )
                except SessionExpiredError as e:
                    log(f"[LINE] ⚠️ session 過期(reply profile): {e}")
                    return
                if r_wrote:
                    new_count += 1

            mark_seen(conn, post["url"], notified=op_wrote)

    log(f"[LINE] 本輪新增 {new_count} 個帳號")


async def main() -> None:
    async with open_browser_session() as (conn, page):
        while True:
            try:
                await run_once(conn, page)
            except Exception:
                log("run_once 例外:\n" + traceback.format_exc())
            try:
                await run_line_lead_once(conn, page)
            except Exception:
                log("run_line_lead_once 例外:\n" + traceback.format_exc())
            log(f"休息 {SCAN_INTERVAL_SECONDS}s")
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
