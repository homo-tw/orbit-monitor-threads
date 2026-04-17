import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "900"))
MAX_AGE_DAYS = int(os.getenv("MAX_AGE_DAYS", "5"))

PROXY_SERVER = os.getenv("PROXY_SERVER", "").strip()
PROXY_USERNAME = os.getenv("PROXY_USERNAME", "").strip()
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD", "").strip()


def get_proxy_config() -> dict | None:
    if not PROXY_SERVER:
        return None
    cfg = {"server": PROXY_SERVER}
    if PROXY_USERNAME:
        cfg["username"] = PROXY_USERNAME
    if PROXY_PASSWORD:
        cfg["password"] = PROXY_PASSWORD
    return cfg

# 粗撈關鍵字:寧可多撈,LLM 再過濾。可自行增減。
KEYWORDS = [
    "預約系統",
    "訂位系統",
    "線上預約",
    "預約 推薦",
    "訂位 推薦",
]

# LLM 判斷目標:在找 / 想買 / 求推薦「預約系統」
CLASSIFIER_SYSTEM_PROMPT = """你是一個分類器,判斷 Threads 貼文作者是否正在「主動尋找 / 詢問 / 想購買」預約系統、訂位系統、排程系統、線上預約工具等解決方案。

只在作者明顯表達「需求 / 求推薦 / 想導入 / 找廠商」時才判定為 match。
以下情況 NOT match:
- 作者是廠商在宣傳自家產品
- 單純抱怨某系統但沒在找替代品
- 只是提到預約行為(例如「我預約了餐廳」)

回傳嚴格 JSON:{"match": true|false, "confidence": 0.0~1.0, "reason": "一句話中文理由"}"""

STORAGE_STATE_PATH = "storage_state.json"
DB_PATH = "posts.db"
