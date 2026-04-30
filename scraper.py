import asyncio
from urllib.parse import quote


class SessionExpiredError(Exception):
    pass


SEARCH_URL = "https://www.threads.com/search?q={q}&serp_type=default"
PROFILE_URL = "https://www.threads.com/@{username}"

PROFILE_EXTRACT_JS = r"""
() => {
  const FOLLOWER_RE = /follower|粉絲|粉丝|フォロワー/i;

  function pickBioByFollower() {
    // 找含 follower 文字的最內層元素(子元素都不含)
    let leaf = null;
    for (const el of document.querySelectorAll('*')) {
      const t = (el.innerText || '').trim();
      if (!t || t.length > 80) continue;
      if (!FOLLOWER_RE.test(t)) continue;
      if (![...el.children].some(c => FOLLOWER_RE.test(c.innerText || ''))) {
        leaf = el;
      }
    }
    if (!leaf) return '';

    // 上 5 層 parent
    let p = leaf;
    for (let i = 0; i < 5; i++) {
      if (!p.parentElement) return '';
      p = p.parentElement;
    }

    // 在 5 層 parent 的 children 裡反找含 follower 的 idx,取它前面 2 個 children 當 bio
    const kids = [...p.children];
    let idx = -1;
    for (let i = kids.length - 1; i >= 0; i--) {
      if (FOLLOWER_RE.test(kids[i].innerText || '')) {
        idx = i;
        break;
      }
    }
    if (idx < 1) return '';
    const start = Math.max(0, idx - 2);
    return kids.slice(start, idx)
      .map(c => (c.innerText || '').trim())
      .filter(Boolean)
      .join('\n')
      .trim();
  }

  let bio = pickBioByFollower();
  if (!bio) {
    const og = document.querySelector('meta[property="og:description"]');
    if (og) bio = (og.getAttribute('content') || '').trim();
    if (!bio) {
      const d = document.querySelector('meta[name="description"]');
      if (d) bio = (d.getAttribute('content') || '').trim();
    }
  }

  const links = [];
  document.querySelectorAll('a[href^="http"]').forEach(a => {
    const h = a.getAttribute('href') || '';
    if (!h) return;
    if (h.includes('threads.net') || h.includes('threads.com')) return;
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
    # 等 follower 字樣 render(JS 渲染才有,沒有 SSR);沒等到也不擋,fallback og:description
    try:
        await page.wait_for_function(
            """() => /follower|粉絲|粉丝|フォロワー/i.test(document.body.innerText)""",
            timeout=10000,
        )
    except Exception:
        pass
    await asyncio.sleep(1)
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


async def scrape_post_replies(page, op_post_url: str, scrolls: int = 5):
    """進貼文詳情頁,滾動載入 replies,回傳 reply 清單(扣除 OP 本身)。
    op_post_url 是 /@author/post/id 形式的相對 URL。"""
    full_url = f"https://www.threads.com{op_post_url}"
    await page.goto(full_url, wait_until="domcontentloaded", timeout=30000)
    current = page.url
    if "/login" in current or "/accounts/login" in current:
        raise SessionExpiredError(f"post 詳情頁被導到登入頁: {current}")

    try:
        await page.wait_for_selector('a[href*="/post/"]', timeout=8000)
    except Exception:
        return []

    for _ in range(scrolls):
        await page.mouse.wheel(0, 4000)
        await asyncio.sleep(3)

    posts = await page.evaluate(EXTRACT_JS)
    # 扣掉 OP 自己;reply 文字常很短(像「lin.ee/xxx」),不套用長度過濾
    return [p for p in posts if p.get("author") and p.get("url") != op_post_url]
