"""Cron 進入點:只跑 pipeline 1(找需要預約系統的店家 → Discord 通知)。
建議排程 15 分鐘一次。"""
import asyncio

from main import log_startup, open_browser_session, run_once


async def amain() -> None:
    async with open_browser_session() as (conn, page):
        await run_once(conn, page)


if __name__ == "__main__":
    log_startup("booking")
    asyncio.run(amain())
