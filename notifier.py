import requests
from config import DISCORD_WEBHOOK_URL


def notify(post: dict, classification: dict) -> bool:
    if not DISCORD_WEBHOOK_URL:
        print("[notifier] DISCORD_WEBHOOK_URL 未設定,跳過")
        return False

    full_url = f"https://www.threads.net{post['url']}"
    text = post.get("text", "")
    snippet = text[:500] + ("..." if len(text) > 500 else "")

    embed = {
        "title": f"@{post['author']} 可能在找預約系統",
        "description": snippet,
        "url": full_url,
        "fields": [
            {"name": "AI 判斷理由", "value": classification.get("reason", "-")[:1000]},
            {
                "name": "信心度",
                "value": f"{classification.get('confidence', 0):.0%}",
                "inline": True,
            },
        ],
    }

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[notifier] webhook 失敗: {e}")
        return False
