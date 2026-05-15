import asyncio
import os
import re
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.async_api import async_playwright

from config import (
    KEYWORDS,
    LINE_LEAD_KEYWORDS,
    LINE_LEAD_MAX_AGE_DAYS,
    LINE_LEAD_SCROLLS,
    SCAN_INTERVAL_SECONDS,
    MAX_AGE_DAYS,
    MATCH_CONFIDENCE_THRESHOLD,
    STORAGE_STATE_PATH,
    DB_PATH,
    get_proxy_config,
)
from storage import init_db, is_seen, mark_seen
from scraper import (
    SessionExpiredError,
    fetch_instagram_follower_count,
    fetch_instagram_profile,
    fetch_threads_profile,
    scrape_keyword,
    scrape_post_replies,
)
from places import PlacesConfigError, search_places
from classifier import classify
from notifier import notify_batch, notify_alert
from line_lead import (
    bio_mentions_line,
    extract_line_url,
    extract_line_via_llm,
    load_cache,
    normalize_handle,
    resolve_line_id_url,
    save_account,
)

ALERT_MARKER = ".session_alert_sent"
ALERT_THROTTLE_HOURS = 6
CRON_LOG = Path(__file__).parent / "cron.log"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def log_startup(label: str) -> None:
    """每個 cron 進入點啟動時 append 一行到 cron.log,確認排程有觸發。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(CRON_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] started ({label})\n")
    except Exception:
        pass


@asynccontextmanager
async def open_browser_session():
    """共用 browser setup:proxy / storage state / 單一 page。yield (conn, page)。"""
    conn = init_db(DB_PATH)
    proxy = get_proxy_config()
    async with async_playwright() as p:
        launch_kwargs = {"headless": True}
        if proxy:
            launch_kwargs["proxy"] = proxy
            log(f"使用 proxy: {proxy['server']}")
        browser = await p.chromium.launch(**launch_kwargs)
        try:
            ctx_kwargs = {}
            if os.path.exists(STORAGE_STATE_PATH):
                ctx_kwargs["storage_state"] = STORAGE_STATE_PATH
                log(f"使用 {STORAGE_STATE_PATH} 登入 session")
            else:
                log("沒有 storage_state.json,以匿名瀏覽(部分內容可能受限)")
            context = await browser.new_context(**ctx_kwargs)
            page = await context.new_page()
            yield conn, page
        finally:
            await browser.close()


def _should_alert_session() -> bool:
    if not os.path.exists(ALERT_MARKER):
        return True
    age = time.time() - os.path.getmtime(ALERT_MARKER)
    return age > ALERT_THROTTLE_HOURS * 3600


def _mark_session_alerted() -> None:
    open(ALERT_MARKER, "w").close()


def _clear_session_alert() -> None:
    if os.path.exists(ALERT_MARKER):
        os.remove(ALERT_MARKER)


def _is_recent(post: dict, cutoff: datetime) -> bool:
    ts = post.get("published")
    if not ts:
        return False  # 沒時間戳就跳過,避免撈到太舊的
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    return dt >= cutoff


async def run_once(conn, page) -> None:
    matches: list[tuple[dict, dict]] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

    for kw in KEYWORDS:
        log(f"搜尋關鍵字: {kw}")
        try:
            posts = await scrape_keyword(page, kw)
        except SessionExpiredError as e:
            log(f"⚠️ session 過期: {e}")
            if _should_alert_session():
                notify_alert(
                    "⚠️ Threads session 已過期,本機重跑 `python login.py` "
                    "後把新的 `storage_state.json` 上傳到 server"
                )
                _mark_session_alerted()
            return
        except Exception as e:
            log(f"  scrape 失敗: {e}")
            continue
        log(f"  抓到 {len(posts)} 則")

        recent = [p for p in posts if _is_recent(p, cutoff)]
        if len(recent) < len(posts):
            log(f"  過濾後 {len(recent)} 則在近 {MAX_AGE_DAYS} 天內")

        for post in recent:
            if is_seen(conn, post["url"]):
                continue
            try:
                result = classify(post["text"])
            except Exception as e:
                log(f"  classify 失敗: {e}")
                continue

            if result["match"] and result["confidence"] >= MATCH_CONFIDENCE_THRESHOLD:
                matches.append((post, result))
                log(
                    f"  ✅ match @{post['author']} "
                    f"(conf={result['confidence']:.2f}) {post['url']}"
                )
            else:
                if result["match"]:
                    log(
                        f"  ⚠️ match 但 confidence {result['confidence']:.2f} "
                        f"< {MATCH_CONFIDENCE_THRESHOLD},略過: {post['url']}"
                    )
                mark_seen(conn, post["url"], notified=False)

    # 能順利跑完所有關鍵字代表 session 還活著,清掉警示節流
    _clear_session_alert()

    if not matches:
        log("本輪無 match")
        return

    results = notify_batch(matches)
    for (post, _), ok in zip(matches, results):
        mark_seen(conn, post["url"], notified=ok)
    log(f"通知 {sum(results)}/{len(results)} 則")


# 接預約訊號:中文直接 substring,英文用 word boundary(避免誤命中 admin/random)
_BOOKING_CJK = ("預約", "預訂", "私訊")
_BOOKING_EN_RE = re.compile(r"\b(dm|book|booking)\b", re.IGNORECASE)


def _has_booking_signal(text: str) -> bool:
    if not text:
        return False
    if any(k in text for k in _BOOKING_CJK):
        return True
    return bool(_BOOKING_EN_RE.search(text))


def _is_hk_username(username: str) -> bool:
    """username 結尾 hk 或含 _hk / .hk / hk_ / hk. 視為香港帳號。"""
    u = username.lower()
    return (
        u.endswith("hk")
        or "_hk" in u
        or ".hk" in u
        or "hk_" in u
        or "hk." in u
    )


async def _process_author_candidate(
    page,
    cache: dict,
    checked_authors: set,
    author: str,
    primary_text: str,
    extra_booking_context: str = "",
    source_prefix: str = "",
) -> bool:
    """處理一個 candidate author:fetch profile → 預約 filter → LINE extract → 寫 sheet。
    回傳 True 表示有寫入新一筆,False 表示略過/已存在/失敗。
    SessionExpiredError 會直接 raise 給 caller 處理。"""
    author_key = author.lower()
    if author_key in cache or author_key in checked_authors:
        return False
    checked_authors.add(author_key)

    if _is_hk_username(author):
        log(f"[LINE]   @{author} HK 帳號,略過")
        return False

    try:
        bio, search_blob, follower_count, ig_username = await fetch_threads_profile(
            page, author
        )
    except SessionExpiredError:
        raise
    except Exception as e:
        log(f"[LINE]   fetch_profile @{author} 失敗: {e}")
        bio, search_blob, follower_count, ig_username = "", "", "", ""

    # 接預約訊號 filter:primary_text(post/reply 文)、extra(reply 場景下傳入 OP 文)、bio
    # 至少一個含「預約 / 預訂 / 私訊 / DM / book / booking」才算服務商家
    if not (
        _has_booking_signal(primary_text)
        or _has_booking_signal(extra_booking_context)
        or _has_booking_signal(bio)
    ):
        log(f"[LINE]   @{author} 無預約/預訂/私訊/DM/book 訊號,略過")
        return False

    raw_line_url = extract_line_url(search_blob)
    source = f"{source_prefix}profile-url"

    if not raw_line_url:
        raw_line_url = extract_line_url(primary_text)
        if raw_line_url:
            source = f"{source_prefix}post-url"

    line_url = ""
    if raw_line_url:
        line_url = resolve_line_id_url(raw_line_url)
        if not line_url:
            log(f"[LINE]   @{author} {raw_line_url} 無法展開成 @ID,略過")
            return False
    else:
        if not bio_mentions_line(bio):
            log(f"[LINE]   @{author} bio 沒提到 LINE,略過 LLM")
            return False
        llm_result = await extract_line_via_llm(bio)
        if not llm_result:
            log(f"[LINE]   @{author} 無 LINE(regex/LLM 都沒),略過")
            return False
        if "lin.ee" in llm_result.lower() or "line.me" in llm_result.lower():
            line_url = resolve_line_id_url(llm_result)
            if not line_url:
                log(f"[LINE]   @{author} LLM 給 {llm_result} 但無法展開,略過")
                return False
            source = f"{source_prefix}llm-url"
        else:
            # @xxx 文字版 — 比對 author / IG handle,避免 LLM 把原 username 當 LINE
            normalized = normalize_handle(llm_result)
            if len(normalized) < 3:
                log(f"[LINE]   @{author} LLM 回 {llm_result!r} 不像 LINE ID,略過")
                return False
            ig_norm = ig_username.lower() if ig_username else ""
            if normalized == author_key or (ig_norm and normalized == ig_norm):
                log(
                    f"[LINE]   @{author} LLM 回 {llm_result!r} 等於 Threads/IG handle,略過"
                )
                return False
            # 保留原始字形,直接當 L 欄值
            line_url = llm_result
            source = f"{source_prefix}llm-text"

    profile_url = f"https://www.threads.com/@{author}"

    ig_handle = ig_username or author
    try:
        ig_follower_count = await fetch_instagram_follower_count(page, ig_handle)
    except Exception as e:
        log(f"[LINE]   fetch IG follower @{ig_handle} 失敗: {e}")
        ig_follower_count = ""

    try:
        wrote = save_account(
            author_key,
            bio[:500],
            profile_url,
            line_url,
            follower_count,
            ig_follower_count,
            cache,
        )
        if wrote:
            log(f"[LINE]   ✅ 寫入 @{author} {line_url} ({source})")
        else:
            log(f"[LINE]   ⏭️ @{author} 或 {line_url} 已存在 sheet")
        return wrote
    except Exception as e:
        log(f"[LINE]   寫 sheet 失敗 @{author}: {e}")
        return False


async def run_line_lead_once(conn, page) -> None:
    if not LINE_LEAD_KEYWORDS:
        return

    cache = load_cache()
    cutoff = datetime.now(timezone.utc) - timedelta(days=LINE_LEAD_MAX_AGE_DAYS)
    checked_authors: set[str] = set()  # 同一輪內同 author 只抓一次 profile
    new_count = 0

    for kw in LINE_LEAD_KEYWORDS:
        log(f"[LINE] 搜尋: {kw}")
        try:
            posts = await scrape_keyword(page, kw, scrolls=LINE_LEAD_SCROLLS)
        except SessionExpiredError as e:
            log(f"[LINE] ⚠️ session 過期: {e}")
            return
        except Exception as e:
            log(f"[LINE]   scrape 失敗: {e}")
            continue

        recent = [p for p in posts if _is_recent(p, cutoff)]
        log(f"[LINE]   抓到 {len(posts)} 則,近 {LINE_LEAD_MAX_AGE_DAYS} 天內 {len(recent)} 則")

        for post in recent:
            if is_seen(conn, post["url"]):
                continue
            op_post_text = post.get("text") or ""

            # 1. 處理 OP author(可能就是商家自介)
            try:
                op_wrote = await _process_author_candidate(
                    page, cache, checked_authors,
                    author=post["author"],
                    primary_text=op_post_text,
                )
            except SessionExpiredError as e:
                log(f"[LINE] ⚠️ session 過期(OP profile): {e}")
                return
            if op_wrote:
                new_count += 1

            # 2. 進詳情頁抓 replies(消費者求推薦時,商家會在 reply 自我推銷)
            try:
                replies = await scrape_post_replies(page, post["url"], scrolls=5)
            except SessionExpiredError as e:
                log(f"[LINE] ⚠️ session 過期(replies): {e}")
                return
            except Exception as e:
                log(f"[LINE]   scrape_replies 失敗 {post['url']}: {e}")
                replies = []

            if replies:
                log(f"[LINE]   {post['url']} 抓到 {len(replies)} 個 reply")

            for reply in replies:
                try:
                    r_wrote = await _process_author_candidate(
                        page, cache, checked_authors,
                        author=reply["author"],
                        primary_text=reply.get("text") or "",
                        extra_booking_context=op_post_text,
                        source_prefix="reply-",
                    )
                except SessionExpiredError as e:
                    log(f"[LINE] ⚠️ session 過期(reply profile): {e}")
                    return
                if r_wrote:
                    new_count += 1

            mark_seen(conn, post["url"], notified=op_wrote)

    log(f"[LINE] 本輪新增 {new_count} 個帳號")


_IG_URL_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]+)", re.IGNORECASE)
_IG_NON_USER_PATHS = {"p", "reel", "reels", "explore", "accounts", "stories", "tv"}


def _ig_username_from_url(url: str) -> str:
    if not url:
        return ""
    m = _IG_URL_RE.search(url)
    if not m:
        return ""
    handle = m.group(1).rstrip("/").strip()
    if handle.lower() in _IG_NON_USER_PATHS:
        return ""
    return handle


async def _process_place_candidate(
    page,
    cache: dict,
    checked_places: set,
    place: dict,
) -> bool:
    """處理一筆 Google Places 結果。
    website 是 IG → 進 IG 抓 bio,跟 Threads 路線一樣套 regex+LLM。
    其他情況 → 把 name/summary/website/address 串成 blob 給 regex+LLM。"""
    place_id = place.get("place_id") or ""
    name = place.get("name") or ""
    website = place.get("website_uri") or ""
    summary = place.get("editorial_summary") or ""
    address = place.get("formatted_address") or ""
    maps_url = place.get("maps_uri") or ""

    if not place_id or place_id in checked_places:
        return False
    checked_places.add(place_id)

    ig_username = _ig_username_from_url(website)
    ig_bio = ""
    ig_followers = ""
    if ig_username:
        try:
            ig_bio, ig_followers = await fetch_instagram_profile(page, ig_username)
        except Exception as e:
            log(f"[PLACES]   fetch IG @{ig_username} 失敗 ({name}): {e}")

    if ig_username and ig_bio:
        # IG 有抓到 bio → 跟 Threads 路線一樣用 bio 當 LLM 入料
        bio = ig_bio
        profile_url = f"https://www.instagram.com/{ig_username}/"
        # 用 IG handle 當 dedupe key,跨 Threads/Places 同帳號也不重複寫
        user_key = ig_username.lower()
        search_blob = "\n".join(filter(None, [bio, website, summary, name]))
        llm_input = bio
        label = f"@{ig_username} ({name})"
    else:
        # 沒 IG / IG bio 抓不到 → 把 Places 元資料串起來丟 regex+LLM
        bio = summary
        profile_url = maps_url or website or f"https://maps.google.com/?cid={place_id}"
        user_key = f"places:{place_id}".lower()
        search_blob = "\n".join(filter(None, [name, summary, website, address]))
        llm_input = search_blob
        label = name or place_id

    if user_key in cache:
        return False

    raw_line_url = extract_line_url(search_blob)
    source_tag = "places-url"

    line_url = ""
    if raw_line_url:
        line_url = resolve_line_id_url(raw_line_url)
        if not line_url:
            log(f"[PLACES]   {label} {raw_line_url} 無法展開,略過")
            return False
    else:
        if not bio_mentions_line(llm_input):
            return False
        llm_result = await extract_line_via_llm(llm_input)
        if not llm_result:
            return False
        if "lin.ee" in llm_result.lower() or "line.me" in llm_result.lower():
            line_url = resolve_line_id_url(llm_result)
            if not line_url:
                log(f"[PLACES]   {label} LLM 給 {llm_result} 但無法展開,略過")
                return False
            source_tag = "places-llm-url"
        else:
            normalized = normalize_handle(llm_result)
            if len(normalized) < 3:
                return False
            # 防 LLM 把 IG handle 自己當 LINE ID 回傳
            if ig_username and normalized == ig_username.lower():
                log(f"[PLACES]   {label} LLM 回 {llm_result!r} 等於 IG handle,略過")
                return False
            line_url = llm_result
            source_tag = "places-llm-text"

    try:
        wrote = save_account(
            user_key,
            bio[:500],
            profile_url,
            line_url,
            "",
            ig_followers,
            cache,
            source="google-places",
        )
        if wrote:
            log(f"[PLACES]   ✅ 寫入 {label} → {line_url} ({source_tag})")
        else:
            log(f"[PLACES]   ⏭️ {label} 或 {line_url} 已存在 sheet")
        return wrote
    except Exception as e:
        log(f"[PLACES]   寫 sheet 失敗 {label}: {e}")
        return False


async def run_places_lead_once(conn, page) -> None:
    if not LINE_LEAD_KEYWORDS:
        return

    cache = load_cache()
    checked_places: set[str] = set()
    new_count = 0

    for kw in LINE_LEAD_KEYWORDS:
        log(f"[PLACES] 搜尋: {kw}")
        try:
            results = search_places(kw)
        except PlacesConfigError as e:
            log(f"[PLACES] 設定錯誤,終止: {e}")
            return
        except Exception as e:
            log(f"[PLACES]   search 失敗: {e}")
            continue
        log(f"[PLACES]   抓到 {len(results)} 個 place")

        for place in results:
            try:
                wrote = await _process_place_candidate(page, cache, checked_places, place)
            except Exception as e:
                log(f"[PLACES]   process 失敗 {place.get('name')}: {e}")
                continue
            if wrote:
                new_count += 1

    log(f"[PLACES] 本輪新增 {new_count} 個帳號")


async def main() -> None:
    async with open_browser_session() as (conn, page):
        while True:
            try:
                await run_once(conn, page)
            except Exception:
                log("run_once 例外:\n" + traceback.format_exc())
            try:
                await run_line_lead_once(conn, page)
            except Exception:
                log("run_line_lead_once 例外:\n" + traceback.format_exc())
            log(f"休息 {SCAN_INTERVAL_SECONDS}s")
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
