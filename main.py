import asyncio
import os
import traceback
from datetime import datetime, timedelta, timezone

from playwright.async_api import async_playwright

from config import (
    KEYWORDS,
    SCAN_INTERVAL_SECONDS,
    MAX_AGE_DAYS,
    STORAGE_STATE_PATH,
    DB_PATH,
    get_proxy_config,
)
from storage import init_db, is_seen, mark_seen
from scraper import scrape_keyword
from classifier import classify
from notifier import notify_batch


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


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

            if result["match"]:
                matches.append((post, result))
                log(
                    f"  ✅ match @{post['author']} "
                    f"(conf={result['confidence']:.2f}) {post['url']}"
                )
            else:
                mark_seen(conn, post["url"], notified=False)

    if not matches:
        log("本輪無 match")
        return

    results = notify_batch(matches)
    for (post, _), ok in zip(matches, results):
        mark_seen(conn, post["url"], notified=ok)
    log(f"通知 {sum(results)}/{len(results)} 則")


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
            log(f"休息 {SCAN_INTERVAL_SECONDS}s")
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
