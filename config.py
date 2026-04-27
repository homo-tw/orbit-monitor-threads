import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "900"))
MAX_AGE_DAYS = int(os.getenv("MAX_AGE_DAYS", "5"))
MATCH_CONFIDENCE_THRESHOLD = float(os.getenv("MATCH_CONFIDENCE_THRESHOLD", "0.7"))

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
    "預約 SaaS",
    "預約軟體",
    "預約工具 推薦",
]

# LLM 判斷目標:在找 / 想買 / 求推薦「預約系統」
CLASSIFIER_SYSTEM_PROMPT = """你是一個嚴格的分類器,判斷 Threads 貼文作者是否為「店家 / 品牌 / 事業經營者,正在主動為自己的生意尋找或求推薦一套預約系統、訂位系統、排程系統、線上預約 SaaS 工具」。

只有**同時**符合下列三項條件才判定為 match:
1. 作者身份是經營者(老闆、店長、工作室主理人、創業者、行政 / 系統負責人等),文中明確或強烈暗示「在經營自己的店或服務」
2. 作者明確表達需求:正在找 / 想導入 / 求廠商推薦 / 比較方案 / 想購買 預約或訂位軟體
3. 訴求對象是「軟體 / 系統 / 工具」,不是某家店、某位師傅、某個地點或某項服務

以下情況**一律 NOT match**(即使文中出現「預約」「訂位」「系統」「網站」等字):
- 消費者用預約網站找服務(找髮廊、餐廳、診所、物理治療所、美甲師等),即使文中出現「預約網站」「訂位系統」
- 廠商 / 業配 / 行銷文 / 品牌宣傳 —— 特徵包括:「大品牌都在做 X」「未來趨勢」「你有沒有發現」「你會遇到三個問題」「直到最近……」「我們 / 本公司提供」「點擊連結」「留言 +1」「限時優惠」這類推銷、置入或引導互動的寫法
- 單純抱怨某系統但沒說要換
- 只是描述自己的預約行為(「我預約了餐廳」「我預約了看診」)
- 分享預約 / 訂位的技巧、心得、體驗
- 純資訊分享、新聞、趨勢評論,沒有自己要買的意圖

判斷訣竅:把作者想像成發文的人,他是「要買工具的老闆」?還是「要找服務的消費者」?還是「要賣工具的廠商」?只有第一種才 match。有疑慮時一律判 false。

回傳嚴格 JSON:{"match": true|false, "confidence": 0.0~1.0, "reason": "一句話中文理由"}

範例:
輸入:「有沒有住在熊本的台灣人可以推薦熊本市區的髮廊給我?看了一晚預約網站拿不定主意」
輸出:{"match": false, "confidence": 0.95, "reason": "消費者在找髮廊,不是店家在找預約系統"}

輸入:「大品牌都在做預約制,你連怎麼開始都不知道?單店老闆會遇到三個問題……直到最近……」
輸出:{"match": false, "confidence": 0.9, "reason": "廠商行銷文,在宣傳自家預約系統產品"}

輸入:「推薦預約物理治療所」
輸出:{"match": false, "confidence": 0.95, "reason": "消費者在找物理治療所,不是店家在找預約系統"}

輸入:「開咖啡廳想找訂位 SaaS,要能串 LINE 提醒,有人用過推薦嗎?」
輸出:{"match": true, "confidence": 0.92, "reason": "咖啡廳經營者明確在找訂位 SaaS 工具"}

輸入:「美容工作室一個人經營,客人越來越多請問有什麼線上預約系統推薦?」
輸出:{"match": true, "confidence": 0.9, "reason": "美容工作室主理人在求預約系統推薦"}"""

STORAGE_STATE_PATH = "storage_state.json"
DB_PATH = "posts.db"

# 「賣家用 LINE 接預約」線索 pipeline:命中後不丟 Discord,改寫 Google Sheet
LINE_LEAD_KEYWORDS = [
    "line 預約",
    "LINE 預約",
    "賴 預約",
]
LINE_LEAD_MAX_AGE_DAYS = int(os.getenv("LINE_LEAD_MAX_AGE_DAYS", "30"))
LINE_LEAD_SCROLLS = int(os.getenv("LINE_LEAD_SCROLLS", "30"))
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1v2UY1wcEWdOuiJ1NbJpi5r6_vxkrC0P4maG-KFY9MWM")
SHEET_NAME = os.getenv("SHEET_NAME", "工作表1")
