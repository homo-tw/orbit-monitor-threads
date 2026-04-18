import requests
from config import DISCORD_WEBHOOK_URL

HEADER = "以下是最近可能有類似需求的貼文:"
WEBHOOK_USERNAME = "Orbit 海巡號"


def notify_batch(items: list[tuple[dict, dict]]) -> list[bool]:
    if not items:
        return []
    if not DISCORD_WEBHOOK_URL:
        print("[notifier] DISCORD_WEBHOOK_URL 未設定,跳過")
        return [False] * len(items)

    lines = [
        f"- [@{post['author']}](https://www.threads.net{post['url']})"
        for post, _ in items
    ]
    results = [False] * len(items)

    # Discord 單則訊息 content 上限 2000 字,分批送;第一批加 header
    batch: list[int] = []
    batch_len = len(HEADER) + 1
    first = True
    for i, line in enumerate(lines):
        add = len(line) + 1
        if batch and batch_len + add > 1900:
            _flush(batch, lines, results, with_header=first)
            first = False
            batch, batch_len = [], 0
        batch.append(i)
        batch_len += add
    if batch:
        _flush(batch, lines, results, with_header=first)

    return results


def _flush(
    idxs: list[int], lines: list[str], results: list[bool], with_header: bool
) -> None:
    body = "\n".join(lines[i] for i in idxs)
    content = f"{HEADER}\n{body}" if with_header else body
    try:
        r = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": content, "username": WEBHOOK_USERNAME},
            timeout=10,
        )
        r.raise_for_status()
        for i in idxs:
            results[i] = True
    except Exception as e:
        print(f"[notifier] webhook 失敗: {e}")


def notify_alert(message: str) -> bool:
    if not DISCORD_WEBHOOK_URL:
        print("[notifier] DISCORD_WEBHOOK_URL 未設定,跳過")
        return False
    try:
        r = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": message, "username": WEBHOOK_USERNAME},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[notifier] alert 失敗: {e}")
        return False
