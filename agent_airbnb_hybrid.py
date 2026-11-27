#!/usr/bin/env python3
"""
混合式：Playwright 直接從 DOM 抓高雄苓雅區 Airbnb 房源（不經過 LLM），
只有在直接抓不到時，才讓 browser-use Agent（Ollama）幫忙走到搜尋結果頁。

安裝：
    pip install playwright python-dotenv pydantic
    playwright install chromium
    # 只有要用 LLM 後備時才需要：
    pip install "browser-use==0.7.10"
    ollama pull qwen2.5vl:7b

執行：
    python airbnb_hybrid.py
    set CHECKIN=2027-01-10 & set CHECKOUT=2027-01-12 & python airbnb_hybrid.py

環境變數（皆可選）：
    DEST           目的地，預設 Lingya District, Kaohsiung, Taiwan
    CHECKIN        入住日 YYYY-MM-DD（可不填；不填時 Airbnb 通常顯示每晚價）
    CHECKOUT       退房日 YYYY-MM-DD
    CURRENCY       希望顯示的貨幣代碼，預設 TWD（Airbnb 不一定理會，程式會偵測實際貨幣）
    HEADLESS       1 = 不顯示視窗，預設 0
    MIN_LISTINGS   至少抓幾筆，預設 40
    MAX_PAGES      最多翻幾頁（每頁約 18 筆），預設 4
    MIN_PRICE      低於此價格視為異常並排除，預設 300（TWD）
    KEEP_OPEN      1 = 跑完後不關閉瀏覽器，按 Enter 才結束，預設 1
    OPEN_CHEAPEST  1 = 跑完後在瀏覽器開啟最便宜的房源，預設 1
    USE_AGENT_FALLBACK  1 = 直接抓失敗時啟用 LLM Agent 後備，預設 1
    OLLAMA_MODEL / OLLAMA_HOST / OLLAMA_NUM_CTX / USE_VISION / MAX_STEPS  （僅後備用）
"""
import asyncio
import datetime as dt
import inspect
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from typing import List, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

DEST = os.getenv("DEST", "Lingya District, Kaohsiung, Taiwan")
CHECKIN = os.getenv("CHECKIN", "")
CHECKOUT = os.getenv("CHECKOUT", "")
CURRENCY = os.getenv("CURRENCY", "TWD")
HEADLESS = os.getenv("HEADLESS", "0") == "1"
MIN_LISTINGS = int(os.getenv("MIN_LISTINGS", "40"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "4"))
MIN_PRICE = float(os.getenv("MIN_PRICE", "300"))
USE_AGENT_FALLBACK = os.getenv("USE_AGENT_FALLBACK", "1") == "1"
KEEP_OPEN = os.getenv("KEEP_OPEN", "1") == "1"
OPEN_CHEAPEST = os.getenv("OPEN_CHEAPEST", "1") == "1"

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5vl:7b")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "32768"))
USE_VISION = os.getenv("USE_VISION", "1") == "1"
MAX_STEPS = int(os.getenv("MAX_STEPS", "20"))


# --------------------------------------------------------------------------
# 資料模型
# --------------------------------------------------------------------------
class AirbnbListing(BaseModel):
    title: str
    price_per_night: float
    currency: str = "?"
    rating: Optional[float] = None
    url: Optional[str] = None


class SearchResult(BaseModel):
    location: str = Field(default=DEST)
    listings: List[AirbnbListing]


# --------------------------------------------------------------------------
# 解析（純 Python，不用 LLM）
# --------------------------------------------------------------------------
CURRENCY_SYMBOLS = {
    "NT$": "TWD", "US$": "USD", "$": "USD", "€": "EUR", "£": "GBP",
    "¥": "JPY", "kr": "SEK", "SEK": "SEK", "TWD": "TWD", "USD": "USD",
    "EUR": "EUR", "GBP": "GBP",
}
_NUM = r"\d[\d,.\u00a0\u202f ]*"
PRICE_RE = re.compile(
    rf"(?:(NT\$|US\$|[$€£¥]|SEK|TWD|USD|EUR|GBP)\s?({_NUM}))"
    rf"|(?:({_NUM})\s?(kr|SEK)\b)"
)


def _to_number(s: str) -> Optional[float]:
    s = re.sub(r"[\s\u00a0\u202f,]", "", s).rstrip(".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_price(text: str, default_nights: int):
    """回傳 (每晚價格, 貨幣)；抓不到回傳 (None, None)。"""
    matches = list(PRICE_RE.finditer(text))
    if not matches:
        return None, None
    m = matches[-1]  # 有折扣時，前面是劃掉的原價，最後一個才是現價
    if m.group(1):
        symbol, num = m.group(1), m.group(2)
    else:
        symbol, num = m.group(4), m.group(3)
    value = _to_number(num)
    if not value:
        return None, None
    currency = CURRENCY_SYMBOLS.get(symbol, symbol)
    if symbol == "$" and CURRENCY in ("TWD", "USD", "HKD", "SGD", "AUD", "CAD"):
        currency = CURRENCY  # 單純的 "$" 有歧義，以請求的貨幣為準

    if re.search(r"\btotal\b", text, re.I) or re.search(r"總(價|共)", text):
        n = re.search(r"(\d+)\s*nights?", text, re.I)
        nights = int(n.group(1)) if n else default_nights
        if nights > 1:
            value = value / nights
    return round(value, 2), currency


def parse_rating(text: str) -> Optional[float]:
    m = re.search(r"\b([1-5][.,]\d{1,2})\b", text)
    return float(m.group(1).replace(",", ".")) if m else None


def nights_between(a: str, b: str) -> int:
    try:
        d = (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days
        return max(d, 1)
    except Exception:
        return 1


def build_search_url() -> str:
    parts = [x.strip().replace(" ", "-") for x in DEST.split(",") if x.strip()]
    slug = urllib.parse.quote("--".join(parts))
    params = {"adults": "1", "currency": CURRENCY}
    if CHECKIN and CHECKOUT:
        params["checkin"] = CHECKIN
        params["checkout"] = CHECKOUT
    return f"https://www.airbnb.com/s/{slug}/homes?{urllib.parse.urlencode(params)}"


# --------------------------------------------------------------------------
# 瀏覽器操作（Playwright，直接讀 DOM）
# --------------------------------------------------------------------------
JS_COLLECT = """
() => {
  const out = [];
  const seen = new Set();
  document.querySelectorAll('[data-testid="card-container"]').forEach(card => {
    const a = card.querySelector('a[href*="/rooms/"]');
    if (!a) return;
    const m = a.href.match(/\\/rooms\\/(\\d+)/);
    if (!m || seen.has(m[1])) return;
    seen.add(m[1]);
    const t = card.querySelector('[data-testid="listing-card-title"]');
    out.push({
      id: m[1],
      title: (t ? t.innerText : (a.getAttribute('aria-label') || '')).trim(),
      text: card.innerText || ''
    });
  });
  return out;
}
"""


async def dismiss_popups(page) -> None:
    for label in ("Accept all", "Accept", "OK", "Got it", "Close"):
        try:
            btn = page.get_by_role("button", name=label, exact=True).first
            if await btn.is_visible(timeout=800):
                await btn.click(timeout=1500)
        except Exception:
            pass
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass


_pw = None
_browser = None
_page = None


async def close_browser() -> None:
    global _pw, _browser, _page
    try:
        if _browser:
            await _browser.close()
    except Exception:
        pass
    try:
        if _pw:
            await _pw.stop()
    except Exception:
        pass
    _pw = _browser = _page = None


async def scrape_url(url: str, want: int) -> List[AirbnbListing]:
    from playwright.async_api import async_playwright

    global _pw, _browser, _page
    nights = nights_between(CHECKIN, CHECKOUT) if CHECKIN and CHECKOUT else 1
    sep = "&" if "?" in url else "?"
    raw: dict = {}

    await close_browser()  # 若是第二次呼叫（後備流程），先關掉上一個
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(headless=HEADLESS)
    ctx = await _browser.new_context(
        locale="en-US", viewport={"width": 1400, "height": 1000})
    page = _page = await ctx.new_page()
    for page_no in range(MAX_PAGES):
        page_url = url if page_no == 0 else f"{url}{sep}items_offset={page_no * 18}"
        print(f"🌐 第 {page_no + 1}/{MAX_PAGES} 頁：{page_url}")
        await page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
        try:
            await page.wait_for_selector(
                '[data-testid="card-container"]', timeout=25000)
        except Exception:
            print("⚠️ 25 秒內沒看到房源卡片（可能到最後一頁或被擋）")
            await dismiss_popups(page)
        await dismiss_popups(page)

        before = len(raw)
        for _ in range(6):  # 逐步往下捲，觸發 lazy load
            for item in await page.evaluate(JS_COLLECT):
                raw.setdefault(item["id"], item)
            await page.mouse.wheel(0, 1600)
            await page.wait_for_timeout(1000)
        for item in await page.evaluate(JS_COLLECT):
            raw.setdefault(item["id"], item)

        print(f"   累計 {len(raw)} 筆")
        if len(raw) == before:  # 這頁沒有新房源，停止翻頁
            break
        if len(raw) >= want:
            break

    listings: List[AirbnbListing] = []
    for item in raw.values():
        price, cur = parse_price(item["text"], nights)
        if price is None or price < MIN_PRICE:
            continue
        title = item["title"] or item["text"].split("\n")[0]
        listings.append(AirbnbListing(
            title=title,
            price_per_night=price,
            currency=cur or "?",
            rating=parse_rating(item["text"]),
            url=f"https://www.airbnb.com/rooms/{item['id']}",
        ))
    return listings


# --------------------------------------------------------------------------
# LLM 後備：只負責走到搜尋結果頁並回報網址，資料仍由 Playwright 抓
# --------------------------------------------------------------------------
def build_llm():
    try:
        from browser_use import ChatOllama  # 0.x
        return ChatOllama(model=OLLAMA_MODEL, host=OLLAMA_HOST,
                          ollama_options={"num_ctx": OLLAMA_NUM_CTX})
    except ImportError:
        from browser_use import ChatOpenAI  # 1.0
        return ChatOpenAI(OLLAMA_MODEL, base_url=f"{OLLAMA_HOST}/v1", api_key="ollama")


def check_ollama() -> None:
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=5) as r:
            tags = json.load(r)
    except Exception as e:
        sys.exit(f"❌ 連不上 Ollama ({OLLAMA_HOST})：{e}")
    names = [m.get("name", "") for m in tags.get("models", [])]
    wanted = OLLAMA_MODEL if ":" in OLLAMA_MODEL else f"{OLLAMA_MODEL}:latest"
    if wanted not in names:
        sys.exit(f"❌ 找不到模型 {OLLAMA_MODEL}，請執行：ollama pull {OLLAMA_MODEL}")


async def agent_find_results_url() -> Optional[str]:
    from browser_use import Agent, Browser

    check_ollama()
    print(f"🤖 啟用 LLM 後備（{OLLAMA_MODEL}），只負責走到搜尋結果頁")
    task = (
        "Go to https://www.airbnb.com . Close any pop-ups or cookie banners. "
        f"Search for '{DEST}' with the search box and submit. "
        "Wait until listing cards with prices are visible, then stop and "
        "call done. Do NOT extract or read any listing data."
    )
    browser = Browser(headless=HEADLESS)
    supported = inspect.signature(Agent.__init__).parameters
    kwargs = dict(task=task, llm=build_llm(), browser=browser)
    if "use_vision" in supported:
        kwargs["use_vision"] = USE_VISION
    if "step_timeout" in supported:
        kwargs["step_timeout"] = 300
    if "llm_timeout" in supported:
        kwargs["llm_timeout"] = 240
    agent = Agent(**kwargs)
    try:
        history = await agent.run(max_steps=MAX_STEPS)
        urls = [u for u in (history.urls() or []) if u and "/s/" in u]
        return urls[-1] if urls else None
    finally:
        try:
            await browser.stop()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 報表
# --------------------------------------------------------------------------
def print_report(result: SearchResult) -> None:
    listings = [l for l in result.listings if l.price_per_night > 0]
    if not listings:
        print("❌ 沒有擷取到任何有效房源")
        return
    listings.sort(key=lambda x: x.price_per_night)
    prices = [l.price_per_night for l in listings]
    cheapest = listings[0]
    avg = sum(prices) / len(prices)
    cur = cheapest.currency

    print("\n" + "=" * 70)
    print("📊 搜尋結果")
    print("=" * 70)
    print(f"地點        : {result.location}")
    print(f"房源數量    : {len(listings)}")
    print(f"平均價格    : {avg:,.2f} {cur}/晚")
    print(f"價格範圍    : {min(prices):,.0f} - {max(prices):,.0f} {cur}")
    if len({l.currency for l in listings}) > 1:
        print("⚠️ 結果含多種貨幣，統計數字僅供參考")

    print("\n🏆 最便宜的房源")
    print("-" * 70)
    print(f"名稱        : {cheapest.title}")
    print(f"價格        : {cheapest.price_per_night:,.2f} {cur}/晚")
    if cheapest.rating:
        print(f"評分        : ⭐ {cheapest.rating}")
    print(f"比平均省    : {avg - cheapest.price_per_night:,.2f} {cur}")
    print(f"連結        : {cheapest.url or 'N/A'}")

    print("\n📋 全部房源（由低到高）")
    print("-" * 70)
    for i, l in enumerate(listings, 1):
        badge = "🏆" if i == 1 else "  "
        rating = f"⭐{l.rating}" if l.rating else "  N/A"
        title = (l.title[:38] + "…") if len(l.title) > 38 else l.title
        print(f"{badge}{i:>2}. {l.price_per_night:>9,.0f} {l.currency:<4} {rating:<7} {title}")
        if l.url:
            print(f"        {l.url}")


# --------------------------------------------------------------------------
async def main() -> None:
    try:
        listings = await scrape_url(build_search_url(), MIN_LISTINGS)

        if len(listings) < 3 and USE_AGENT_FALLBACK:
            print(f"⚠️ 直接抓取只得到 {len(listings)} 筆，改用 LLM 後備")
            url = await agent_find_results_url()
            if url:
                print(f"✅ Agent 找到結果頁：{url}")
                listings = await scrape_url(url, MIN_LISTINGS)
            else:
                print("❌ Agent 沒能走到搜尋結果頁")

        print_report(SearchResult(location=DEST, listings=listings))

        if KEEP_OPEN and _page is not None:
            if OPEN_CHEAPEST and listings:
                cheapest = min(listings, key=lambda x: x.price_per_night)
                try:
                    await _page.goto(cheapest.url, wait_until="domcontentloaded")
                    print(f"\n🔗 已在瀏覽器開啟最便宜的房源：{cheapest.url}")
                except Exception as e:
                    print(f"⚠️ 無法開啟最便宜房源：{e}")
            print("\n🖥️ 瀏覽器保持開啟，回到這裡按 Enter 才會關閉並結束…")
            await asyncio.to_thread(input)
    finally:
        # 保留模式下，會等使用者按 Enter 後才走到這裡；出錯則立即關閉
        await close_browser()


if __name__ == "__main__":
    asyncio.run(main())
