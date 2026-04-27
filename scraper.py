import asyncio
from urllib.parse import quote


class SessionExpiredError(Exception):
    pass


SEARCH_URL = "https://www.threads.net/search?q={q}&serp_type=default"
PROFILE_URL = "https://www.threads.net/@{username}"

PROFILE_EXTRACT_JS = r"""
() => {
  let bio = '';
  const og = document.querySelector('meta[property="og:description"]');
  if (og) bio = (og.getAttribute('content') || '').trim();
  if (!bio) {
    const d = document.querySelector('meta[name="description"]');
    if (d) bio = (d.getAttribute('content') || '').trim();
  }
  const links = [];
  document.querySelectorAll('a[href^="http"]').forEach(a => {
    const h = a.getAttribute('href') || '';
    if (!h) return;
    if (h.includes('threads.net')) return;
    if (h.includes('instagram.com')) return;
    if (h.includes('facebook.com')) return;
    links.push(h);
  });
  return { bio, links };
}
"""


async def fetch_threads_profile(page, username: str) -> tuple[str, str]:
    """訪問 Threads 個人頁,回傳 (bio, search_blob)。
    search_blob = bio + 所有外部連結串接,給 line_lead.extract_line_url 用。"""
    await page.goto(PROFILE_URL.format(username=username), wait_until="domcontentloaded")
    if "/login" in page.url or "/accounts/login" in page.url:
        raise SessionExpiredError(f"profile 被導到登入頁: {page.url}")
    await asyncio.sleep(2)
    try:
        data = await page.evaluate(PROFILE_EXTRACT_JS)
    except Exception:
        return "", ""
    bio = (data.get("bio") or "").strip()
    links = data.get("links") or []
    search_blob = bio + "\n" + "\n".join(links)
    return bio, search_blob

EXTRACT_JS = r"""
() => {
  const results = [];
  const seen = new Set();
  const links = document.querySelectorAll('a[href*="/post/"]');
  for (const link of links) {
    const href = link.getAttribute('href') || '';
    const m = href.match(/^\/@([^\/]+)\/post\/([^\/\?#]+)/);
    if (!m) continue;
    const canonical = `/@${m[1]}/post/${m[2]}`;
    if (seen.has(canonical)) continue;
    seen.add(canonical);

    // walk up to find a reasonable post container
    let node = link;
    for (let i = 0; i < 8 && node.parentElement; i++) {
      node = node.parentElement;
      const t = (node.innerText || '').trim();
      if (t.length > 40) break;
    }
    const text = (node ? node.innerText : '').trim().slice(0, 2000);

    // 時間:從 container 往下找第一個 <time datetime="...">
    let published = null;
    if (node) {
      const timeEl = node.querySelector('time[datetime]');
      if (timeEl) published = timeEl.getAttribute('datetime');
    }

    results.push({
      url: canonical,
      author: m[1],
      text,
      published,
    });
  }
  return results;
}
"""


async def scrape_keyword(page, keyword: str, scrolls: int = 5):
    url = SEARCH_URL.format(q=quote(keyword))
    await page.goto(url, wait_until="domcontentloaded")

    # 身份過期會被導到登入頁
    current = page.url
    if "/login" in current or "/accounts/login" in current:
        raise SessionExpiredError(f"被導到登入頁: {current}")

    try:
        await page.wait_for_selector('a[href*="/post/"]', timeout=8000)
    except Exception:
        # 沒 post 連結,再確認一次不是登入牆
        needs_login = await page.evaluate(
            """() => !!document.querySelector('input[name="username"], input[name="email"]')"""
        )
        if needs_login:
            raise SessionExpiredError("搜尋頁只看到登入表單")
        return []

    for _ in range(scrolls):
        await page.mouse.wheel(0, 4000)
        await asyncio.sleep(4)

    posts = await page.evaluate(EXTRACT_JS)
    # 過濾太短的雜訊
    return [p for p in posts if len(p.get("text", "")) >= 20]
