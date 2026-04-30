"""Cron 進入點:只跑 pipeline 2(產業關鍵字找 LINE → Google Sheet)。
建議排程 60 分鐘一次(執行約需 20 分鐘)。"""
import asyncio

from main import log_startup, open_browser_session, run_line_lead_once


async def amain() -> None:
    async with open_browser_session() as (conn, page):
        await run_line_lead_once(conn, page)


if __name__ == "__main__":
    log_startup("line_lead")
    asyncio.run(amain())
