import os
import asyncio
import httpx
import random
import time
import traceback
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

# Google GenAI SDK
from google import genai
from google.genai import types
from bs4 import BeautifulSoup

load_dotenv()

app = FastAPI(title="LazyShop - Visual Search Engine")

# CORS Setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- INITIALIZATION ---

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    raise RuntimeError("Missing GOOGLE_API_KEY in .env")

gemini = genai.Client(api_key=GOOGLE_API_KEY)

# --- CONNECTORS ---
CONNECTORS = [
    {
        "name": "Jumia",
        "base_url": "https://www.jumia.com.ng/catalog/?q=",
        "is_active": True,
        "selectors": {
            "item":  "article.prd",
            "title": "h3.name",
            "price": "div.prc",
            "image": "img.img",
            "link":  "a.core",
        },
    },
    {
        "name": "Konga",
        "base_url": "https://www.konga.com/search?search=",
        "is_active": False,
        "selectors": {
            "item":  "div.product-item",
            "title": "span.product-title",
            "price": "span.price",
            "image": "img.product-image",
            "link":  "a.product-link",
        },
    },
]

# Rotate through realistic User-Agent strings to reduce 403s
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

# --- MODELS ---

class SearchRequest(BaseModel):
    image_url: str


# ---------------------------------------------------------------------------
# FIX 1: Gemini 503 — retry with exponential backoff (up to 3 attempts)
# ---------------------------------------------------------------------------

async def analyze_image_with_ai(image_url: str) -> str:
    """
    Identifies the product using a cascade of Gemini models.
    Tries each model up to 2 times before falling back to the next.
    Order: gemini-2.5-flash -> gemini-2.5-flash-lite
    """
    try:
        async with httpx.AsyncClient(timeout=20.0) as http_client:
            image_resp = await http_client.get(image_url)
            image_resp.raise_for_status()
            image_bytes = image_resp.content
    except Exception as e:
        print(f"[AI] Failed to download image: {e}")
        raise RuntimeError("Could not download image") from e

    prompt = (
        "Identify this product. Return only the brand and model name "
        "suitable for a search engine query. Example: 'Nike Air Max 90'. "
        "Be concise — no explanation, just the product name."
    )

    # Try each model in order; fall through to next on 503
    models_to_try = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]

    for model in models_to_try:
        for attempt in range(1, 3):   # 2 attempts per model
            try:
                response = gemini.models.generate_content(
                    model=model,
                    contents=[
                        prompt,
                        types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    ],
                )
                result = response.text.strip()
                print(f"[AI] '{model}' identified (attempt {attempt}): {result}")
                return result

            except Exception as e:
                err_str = str(e)
                is_503 = "503" in err_str or "UNAVAILABLE" in err_str or "high demand" in err_str

                if is_503:
                    if attempt < 2:
                        wait = 2 * attempt
                        print(f"[AI] '{model}' 503, retrying in {wait}s...")
                        await asyncio.sleep(wait)
                    else:
                        print(f"[AI] '{model}' exhausted, trying next model...")
                else:
                    print(f"[AI] '{model}' non-503 error: {e}")
                    break   # skip remaining attempts, move to next model

    print("[AI] All models failed.")
    return None


# ---------------------------------------------------------------------------
# Scraping — Playwright headless browser (handles JS-rendered sites like Jumia)
# ---------------------------------------------------------------------------

def _parse_results(html: str, connector: dict, base_domain: str) -> list:
    """Parse product results from HTML using connector selectors."""
    soup = BeautifulSoup(html, "html.parser")
    sel = connector["selectors"]
    results = []

    for item in soup.select(sel["item"]):
        try:
            title_el = item.select_one(sel["title"])
            price_el = item.select_one(sel["price"])
            image_el = item.select_one(sel["image"])
            link_el  = item.select_one(sel["link"])

            if not (title_el and price_el):
                continue

            img_url = ""
            if image_el:
                img_url = image_el.get("data-src") or image_el.get("src") or ""
                if img_url.startswith("//"):
                    img_url = f"https:{img_url}"

            raw_link = link_el.get("href") if link_el else "#"
            if raw_link and raw_link.startswith("/"):
                full_link = f"{base_domain}{raw_link}"
            else:
                full_link = raw_link or "#"

            results.append({
                "title":  title_el.get_text(strip=True),
                "price":  price_el.get_text(strip=True),
                "image":  img_url,
                "link":   full_link,
                "source": connector["name"],
            })
        except Exception:
            continue

    return results[:10]


def _scrape_in_thread(connector: dict, query: str) -> dict:
    """
    Run Playwright in a dedicated thread with its own subprocess-capable loop.
    This avoids Uvicorn's Windows reload loop, which uses SelectorEventLoop.
    """
    from playwright.async_api import async_playwright

    async def _run() -> dict:
        search_url = f"{connector['base_url']}{query.replace(' ', '+')}"
        base_domain = f"https://{urlparse(connector['base_url']).netloc}"

        try:
            async with async_playwright() as pw:
                async def block_asset(route):
                    await route.abort()

                browser = await pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                context = await browser.new_context(
                    user_agent=random.choice(USER_AGENTS),
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                )
                page = await context.new_page()

                # Block images/fonts/media to speed up load
                await page.route(
                    "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,mp4,mp3}",
                    block_asset,
                )

                print(f"[{connector['name']}] Navigating...")
                await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)

                try:
                    await page.wait_for_selector(connector["selectors"]["item"], timeout=10000)
                except Exception:
                    print(f"[{connector['name']}] Selector not found — page may have changed")

                html = await page.content()
                await context.close()
                await browser.close()

            results = _parse_results(html, connector, base_domain)
            print(f"[{connector['name']}] Found {len(results)} results")
            return {"source": connector["name"], "results": results, "error": None}

        except Exception as e:
            details = str(e).strip() or repr(e)
            print(f"[{connector['name']}] Playwright error ({type(e).__name__}): {details}")
            print(traceback.format_exc())
            return {
                "source": connector["name"],
                "results": [],
                "error": f"{type(e).__name__}: {details}",
            }

    if os.name == "nt":
        policy = asyncio.WindowsProactorEventLoopPolicy()
        loop = policy.new_event_loop()
    else:
        loop = asyncio.new_event_loop()

    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(_run())
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        asyncio.set_event_loop(None)
        loop.close()


async def scrape_site(connector: dict, query: str) -> dict:
    """Run the Playwright scrape off the main server loop."""
    return await asyncio.to_thread(_scrape_in_thread, connector, query)


# --- ROUTES ---

@app.get("/")
async def root():
    active = [c["name"] for c in CONNECTORS if c["is_active"]]
    return {"status": "ok", "active_connectors": active}


@app.get("/connectors")
async def list_connectors():
    return [{"name": c["name"], "is_active": c["is_active"]} for c in CONNECTORS]


@app.post("/search")
async def execute_search(request: SearchRequest):
    # 1. Identify the product via Gemini (with retry)
    product_query = await analyze_image_with_ai(request.image_url)

    if not product_query:
        return {
            "identified_as": None,
            "total": 0,
            "results": [],
            "error": "AI model is temporarily overloaded. Please try again in a few seconds.",
        }

    print(f"[Search] Query: {product_query}")

    # 2. Get active connectors
    active_connectors = [c for c in CONNECTORS if c["is_active"]]
    if not active_connectors:
        return {"identified_as": product_query, "results": [], "error": "No active connectors"}

    # 3. Scrape all active sites in parallel
    tasks = [scrape_site(c, product_query) for c in active_connectors]
    search_results = await asyncio.gather(*tasks)

    # 4. Flatten
    flat_results = [item for result in search_results for item in result["results"]]
    connector_errors = [result for result in search_results if result["error"]]
    error_message = None

    if not flat_results and connector_errors:
        joined_sources = ", ".join(result["source"] for result in connector_errors)
        error_message = f"Search completed, but scraping failed for: {joined_sources}."

    return {
        "identified_as": product_query,
        "total": len(flat_results),
        "results": flat_results,
        "errors": connector_errors,
        "error": error_message,
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True, loop="asyncio")
