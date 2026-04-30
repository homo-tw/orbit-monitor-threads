import asyncio
import os
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from config import STORAGE_STATE_PATH, DB_PATH, get_proxy_config
from storage import init_db
from main import run_once, run_line_lead_once, log

CRON_LOG = Path(__file__).parent / "cron.log"


def _log_startup() -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(CRON_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] started\n")
    except Exception:
        pass


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
        context = await browser.new_context(**ctx_kwargs)
        page = await context.new_page()

        await run_once(conn, page)
        await run_line_lead_once(conn, page)
        await browser.close()


if __name__ == "__main__":
    _log_startup()
    asyncio.run(main())
