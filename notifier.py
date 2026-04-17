import requests
from config import DISCORD_WEBHOOK_URL


def notify_batch(items: list[tuple[dict, dict]]) -> list[bool]:
    if not items:
        return []
    if not DISCORD_WEBHOOK_URL:
        print("[notifier] DISCORD_WEBHOOK_URL 未設定,跳過")
        return [False] * len(items)

    urls = [f"https://www.threads.net{post['url']}" for post, _ in items]
    results = [False] * len(items)

    # Discord 單則訊息 content 上限 2000 字,分批送
    batch: list[int] = []
    batch_len = 0
    for i, url in enumerate(urls):
        add = len(url) + 1  # 換行
        if batch and batch_len + add > 1900:
            _flush(batch, urls, results)
            batch, batch_len = [], 0
        batch.append(i)
        batch_len += add
    if batch:
        _flush(batch, urls, results)

    return results


def _flush(idxs: list[int], urls: list[str], results: list[bool]) -> None:
    content = "\n".join(urls[i] for i in idxs)
    try:
        r = requests.post(
            DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10
        )
        r.raise_for_status()
        for i in idxs:
            results[i] = True
    except Exception as e:
        print(f"[notifier] webhook 失敗: {e}")
