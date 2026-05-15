"""Cron 進入點:只跑 pipeline 3(Google Places → 商家 → IG bio → LINE → Google Sheet)。
建議排程比 run_line_lead 稀疏(例如 6 小時一次),Places 商家流動慢。"""
import asyncio

from main import log_startup, open_browser_session, run_places_lead_once


async def amain() -> None:
    async with open_browser_session() as (conn, page):
        await run_places_lead_once(conn, page)


if __name__ == "__main__":
    log_startup("places")
    asyncio.run(amain())
