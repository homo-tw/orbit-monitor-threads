"""一次性登入腳本:開啟瀏覽器讓你手動登入 Threads,之後存 cookie 重用。

用法:
    python login.py

執行後會開一個瀏覽器,登入完成後回到這個 terminal 按 Enter。
"""
from playwright.sync_api import sync_playwright
from config import STORAGE_STATE_PATH


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://www.threads.com/login")
        input("請在瀏覽器完成登入,然後回到這裡按 Enter 儲存 session...")
        context.storage_state(path=STORAGE_STATE_PATH)
        print(f"已儲存到 {STORAGE_STATE_PATH}")
        browser.close()


if __name__ == "__main__":
    main()
