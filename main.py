import asyncio
import os
import time
import traceback
from datetime import datetime, timedelta, timezone

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
from scraper import scrape_keyword, fetch_threads_profile, SessionExpiredError
from classifier import classify
from notifier import notify_batch, notify_alert
from line_lead import extract_line_url, load_cache, resolve_line_id_url, save_account

ALERT_MARKER = ".session_alert_sent"
ALERT_THROTTLE_HOURS = 6


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


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


async def run_line_lead_once(conn, page) -> None:
    if not LINE_LEAD_KEYWORDS:
        return

    cache = load_cache()
    cutoff = datetime.now(timezone.utc) - timedelta(days=LINE_LEAD_MAX_AGE_DAYS)
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
            author_key = post["author"].lower()

            if author_key in cache:
                mark_seen(conn, post["url"], notified=False)
                continue

            try:
                bio, search_blob = await fetch_threads_profile(page, post["author"])
            except SessionExpiredError as e:
                log(f"[LINE] ⚠️ session 過期(profile): {e}")
                return
            except Exception as e:
                log(f"[LINE]   fetch_profile @{post['author']} 失敗: {e}")
                bio, search_blob = "", ""

            raw_line_url = extract_line_url(search_blob)
            source = "profile"
            sheet_bio = bio

            if not raw_line_url:
                post_text = post.get("text") or ""
                raw_line_url = extract_line_url(post_text)
                if raw_line_url:
                    source = "post"
                    sheet_bio = bio or post_text[:500]

            if not raw_line_url:
                log(f"[LINE]   @{post['author']} 無 LINE 連結(profile/post 都沒),略過")
                mark_seen(conn, post["url"], notified=False)
                continue

            line_url = resolve_line_id_url(raw_line_url)
            if not line_url:
                log(
                    f"[LINE]   @{post['author']} {raw_line_url} 無法展開成 @ID(可能是 LIFF/群組/失效),略過"
                )
                mark_seen(conn, post["url"], notified=False)
                continue

            profile_url = f"https://www.threads.net/@{post['author']}"
            try:
                wrote = save_account(author_key, sheet_bio[:500], profile_url, line_url, cache)
                mark_seen(conn, post["url"], notified=wrote)
                if wrote:
                    new_count += 1
                    log(f"[LINE]   ✅ 寫入 @{post['author']} {line_url} ({source})")
                else:
                    log(f"[LINE]   ⏭️ @{post['author']} 或 {line_url} 已存在 sheet")
            except Exception as e:
                log(f"[LINE]   寫 sheet 失敗 @{post['author']}: {e}")

    log(f"[LINE] 本輪新增 {new_count} 個帳號")


async def main() -> None:
    conn = init_db(DB_PATH)

    proxy = get_proxy_config()

    async with async_playwright() as p:
        launch_kwargs = {"headless": True}
        if proxy:
            launch_kwargs["proxy"] = proxy
            log(f"使用 proxy: {proxy['server']}")
        browser = await p.chromium.launch(**launch_kwargs)

        ctx_kwargs = {}
        if os.path.exists(STORAGE_STATE_PATH):
            ctx_kwargs["storage_state"] = STORAGE_STATE_PATH
            log(f"使用 {STORAGE_STATE_PATH} 登入 session")
        else:
            log("沒有 storage_state.json,以匿名瀏覽(部分內容可能受限)")
        context = await browser.new_context(**ctx_kwargs)
        page = await context.new_page()

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
