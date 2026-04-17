import asyncio
from urllib.parse import quote

SEARCH_URL = "https://www.threads.net/search?q={q}&serp_type=default"

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

    results.push({
      url: canonical,
      author: m[1],
      text,
    });
  }
  return results;
}
"""


async def scrape_keyword(page, keyword: str, scrolls: int = 3):
    url = SEARCH_URL.format(q=quote(keyword))
    await page.goto(url, wait_until="domcontentloaded")
    try:
        await page.wait_for_selector('a[href*="/post/"]', timeout=8000)
    except Exception:
        return []

    for _ in range(scrolls):
        await page.mouse.wheel(0, 4000)
        await asyncio.sleep(1.5)

    posts = await page.evaluate(EXTRACT_JS)
    # 過濾太短的雜訊
    return [p for p in posts if len(p.get("text", "")) >= 20]
