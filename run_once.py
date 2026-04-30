"""手動測試用:跑一次 pipeline 1 + pipeline 2。Cron 用 run_booking.py / run_line_lead.py。"""
import asyncio

from main import log_startup, open_browser_session, run_line_lead_once, run_once


async def amain() -> None:
    async with open_browser_session() as (conn, page):
        await run_once(conn, page)
        await run_line_lead_once(conn, page)


if __name__ == "__main__":
    log_startup("manual")
    asyncio.run(amain())
