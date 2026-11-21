"""HTTP + headless-browser fetching, built to run unattended for weeks.

* Plain HTTP (httpx) for JSON APIs and simple pages — fast, cheap, thread-safe.
* One BrowserWorker thread owns Playwright's Chromium. Any store thread can ask it to
  render a page; requests are queued, so a 1 GB VM never runs two heavy pages at once.
  Images/fonts/media are blocked (less RAM, faster loads) and the browser is recycled
  every N pages so slow memory leaks can't build up over days.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future

import httpx

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}
BLOCK_CODES = {401, 403, 429, 503}


class FetchError(Exception):
    def __init__(self, msg: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(msg)
        self.status = status
        self.retry_after = retry_after


def _retry_after(r: httpx.Response) -> float | None:
    v = r.headers.get("retry-after")
    try:
        return float(v) if v else None
    except ValueError:
        return None


class BrowserWorker(threading.Thread):
    def __init__(self, recycle_after: int = 60, block_resources: bool = True):
        super().__init__(name="browser", daemon=True)
        self.q: queue.Queue = queue.Queue()
        self.recycle_after = recycle_after
        self.block_resources = block_resources
        self._pw = self._browser = self._ctx = None
        self._pages_done = 0
        self.last_beat = time.time()

    # called from any thread
    def render(self, url: str, wait_ms: int = 2500, timeout: float = 120) -> str:
        fut: Future = Future()
        self.q.put((url, wait_ms, fut))
        return fut.result(timeout=timeout)

    def run(self):
        while True:
            try:
                url, wait_ms, fut = self.q.get(timeout=30)
            except queue.Empty:
                self.last_beat = time.time()
                continue
            self.last_beat = time.time()
            if fut.cancelled():
                continue
            try:
                fut.set_result(self._render(url, wait_ms))
            except Exception as e:  # noqa: BLE001 — hand any failure to the caller
                if not isinstance(e, FetchError):
                    log.warning("browser error on %s: %s — restarting browser", url, e)
                    self._shutdown()
                fut.set_exception(e if isinstance(e, FetchError) else FetchError(f"browser: {e}"))
            self.last_beat = time.time()

    def _ensure(self):
        if self._ctx:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu", "--js-flags=--max-old-space-size=256"],
        )
        self._ctx = self._browser.new_context(user_agent=UA, locale="en-IN", viewport={"width": 1366, "height": 900})
        if self.block_resources:
            self._ctx.route("**/*", lambda route: route.abort()
                            if route.request.resource_type in ("image", "media", "font") else route.continue_())
        self._pages_done = 0

    def _render(self, url: str, wait_ms: int) -> str:
        self._ensure()
        page = self._ctx.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if resp is not None:
                if resp.status == 404:
                    raise FetchError(f"404 page not found: {url}", 404)
                if resp.status in BLOCK_CODES:
                    raise FetchError(f"HTTP {resp.status} (blocked?) for {url}", resp.status)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001 — busy pages never go idle; that's fine
                pass
            page.wait_for_timeout(wait_ms)
            return page.content()
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
            self._pages_done += 1
            if self._pages_done >= self.recycle_after:
                self._shutdown()

    def _shutdown(self):
        for obj, meth in ((self._ctx, "close"), (self._browser, "close"), (self._pw, "stop")):
            try:
                if obj:
                    getattr(obj, meth)()
            except Exception:  # noqa: BLE001
                pass
        self._pw = self._browser = self._ctx = None


class Fetcher:
    def __init__(self, use_browser: bool = True, timeout: float = 20.0):
        self.http = httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True,
                                 limits=httpx.Limits(max_connections=40, max_keepalive_connections=20))
        self.use_browser = use_browser and self._playwright_installed()
        self.browser: BrowserWorker | None = None
        if self.use_browser:
            self.browser = BrowserWorker()
            self.browser.start()

    @staticmethod
    def _playwright_installed() -> bool:
        try:
            import playwright  # noqa: F401
            return True
        except ImportError:
            return False

    def browser_available(self) -> bool:
        return self.browser is not None

    # ---- plain HTTP -------------------------------------------------------
    def _get(self, url, params=None, headers=None) -> httpx.Response:
        try:
            return self.http.get(url, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise FetchError(f"{type(e).__name__}: {e}") from e

    def get_json(self, url: str, params: dict | None = None):
        r = self._get(url, params, {"Accept": "application/json"})
        if r.status_code != 200:
            raise FetchError(f"HTTP {r.status_code} for {r.url}", r.status_code, _retry_after(r))
        if "json" not in r.headers.get("content-type", ""):
            raise FetchError(f"expected JSON, got {r.headers.get('content-type') or 'nothing'} from {r.url}"
                             " (bot check page?)", r.status_code)
        try:
            return r.json()
        except ValueError as e:
            raise FetchError(f"bad JSON from {r.url}: {e}") from e

    def get_html(self, url: str, render: bool | str = "auto", wait_ms: int = 2500) -> str:
        """render: True = browser, False = HTTP only, "auto" = HTTP first, browser if blocked."""
        if render is True:
            return self.render(url, wait_ms)
        try:
            r = self._get(url)
        except FetchError:
            if render == "auto" and self.browser_available():
                return self.render(url, wait_ms)
            raise
        if r.status_code in BLOCK_CODES and render == "auto" and self.browser_available():
            return self.render(url, wait_ms)
        if r.status_code >= 400:
            raise FetchError(f"HTTP {r.status_code} for {url}", r.status_code, _retry_after(r))
        return r.text

    def render(self, url: str, wait_ms: int = 2500) -> str:
        if not self.browser:
            raise FetchError("needs a browser: pip install playwright && playwright install chromium")
        return self.browser.render(url, wait_ms)

    def close(self):
        self.http.close()
