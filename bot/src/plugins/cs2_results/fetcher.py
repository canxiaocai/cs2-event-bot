"""HLTV browser fetcher with caching, throttling, and priority-aware access.

HLTV rejects ordinary HTTP clients, so all traffic goes through one long-lived
Chromium process.  Each HTML fetch still uses a fresh browser context because
reusing a context for consecutive HLTV pages is considerably more likely to
trigger Cloudflare.

Network navigations are globally serialized.  The priority gate normally
serves ``live > user > scan > warm`` while aging old requests so decorative
warm-up work cannot be starved forever.  The minimum request gap is applied
once per *fetch*: a Cloudflare challenge retry rides inside the same slot,
because the challenge + reload pair is one logical page load (see
``_navigate_html``).  Logos in a batch are exempt from the gate entirely.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Literal, Optional
from urllib.parse import urlsplit

from nonebot.log import logger

from . import hltv, store
from .config import Config

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

FetchPriority = Literal["live", "user", "scan", "warm"]
PRIORITY_LIVE: FetchPriority = "live"
PRIORITY_USER: FetchPriority = "user"
PRIORITY_SCAN: FetchPriority = "scan"
PRIORITY_WARM: FetchPriority = "warm"

_PRIORITY_RANK: dict[str, int] = {
    PRIORITY_LIVE: 0,
    PRIORITY_USER: 1,
    PRIORITY_SCAN: 2,
    PRIORITY_WARM: 3,
}

# Explicit navigation targets.  Subresources may use third-party services, but
# user-controlled top-level navigation and every redirect stay on HLTV-owned
# hosts.  This is deliberately an exact allowlist rather than ``*.hltv.org``.
_PAGE_HOSTS = frozenset({"hltv.org", "www.hltv.org"})
_ASSET_HOSTS = frozenset({"hltv.org", "www.hltv.org", "img-cdn.hltv.org"})


@dataclass(eq=False, slots=True)
class _Waiter:
    priority: FetchPriority
    sequence: int
    grant_snapshot: int
    future: asyncio.Future[None]
    granted: bool = False


class _FairPriorityGate:
    """A cancellation-safe, starvation-resistant single-owner priority gate.

    Priority is strict for fresh requests.  A waiter moves up one effective
    priority level after every four other grants so scans and warm-ups
    eventually make progress during a long live streak.

    The gate also owns the global navigation clock.  If the request gap has not
    elapsed, nobody becomes owner yet; a lightweight event-loop timer dispatches
    the highest-priority request at the actual navigation deadline.  Thus a
    warm request cannot reserve the gate merely by arriving before a live one
    and then sleeping for a long throttle interval.
    """

    _AGE_GRANTS = 4

    def __init__(self, min_gap: float = 0.0) -> None:
        self._state_lock = asyncio.Lock()
        self._waiters: list[_Waiter] = []
        self._active = False
        self._sequence = 0
        self._grant_count = 0
        self._min_gap = max(0.0, min_gap)
        self._next_grant_at = 0.0
        self._dispatch_timer: asyncio.TimerHandle | None = None
        self._closed = False

    def _effective_rank(self, waiter: _Waiter) -> int:
        grants_waited = self._grant_count - waiter.grant_snapshot
        age_levels = grants_waited // self._AGE_GRANTS
        return max(0, _PRIORITY_RANK[waiter.priority] - age_levels)

    def _timer_dispatch(self) -> None:
        # Event-loop callbacks do not interleave with synchronous critical
        # sections, so it is safe to run the non-awaiting dispatcher here.
        self._dispatch_timer = None
        self._dispatch_locked()

    def _dispatch_locked(self) -> None:
        if self._active or self._closed:
            return
        # A task cancelled while queued also cancels the Future it awaited.
        self._waiters[:] = [w for w in self._waiters if not w.future.done()]
        if not self._waiters:
            return
        now = time.monotonic()
        delay = self._next_grant_at - now
        if delay > 0:
            if self._dispatch_timer is None:
                loop = asyncio.get_running_loop()
                self._dispatch_timer = loop.call_later(delay, self._timer_dispatch)
            return
        if self._dispatch_timer is not None:
            self._dispatch_timer.cancel()
            self._dispatch_timer = None
        waiter = min(
            self._waiters,
            key=lambda w: (self._effective_rank(w), w.sequence),
        )
        self._waiters.remove(waiter)
        self._active = True
        self._grant_count += 1
        self._next_grant_at = now + self._min_gap
        waiter.granted = True
        waiter.future.set_result(None)

    async def acquire(self, priority: FetchPriority) -> None:
        loop = asyncio.get_running_loop()
        async with self._state_lock:
            if self._closed:
                raise RuntimeError("priority gate is closed")
            self._sequence += 1
            waiter = _Waiter(
                priority=priority,
                sequence=self._sequence,
                grant_snapshot=self._grant_count,
                future=loop.create_future(),
            )
            self._waiters.append(waiter)
            self._dispatch_locked()
        try:
            await waiter.future
        except asyncio.CancelledError:
            # Cancellation may race with set_result().  If this waiter had
            # already become owner, hand the gate to the next task here.
            async with self._state_lock:
                if waiter.granted:
                    self._active = False
                    self._dispatch_locked()
                elif waiter in self._waiters:
                    self._waiters.remove(waiter)
            raise

    async def close(self) -> None:
        """Cancel the throttle timer and wake queued callers during shutdown."""
        async with self._state_lock:
            self._closed = True
            if self._dispatch_timer is not None:
                self._dispatch_timer.cancel()
                self._dispatch_timer = None
            waiters, self._waiters = self._waiters, []
            for waiter in waiters:
                if not waiter.future.done():
                    waiter.future.cancel()

    async def release(self) -> None:
        async with self._state_lock:
            if not self._active:
                raise RuntimeError("priority gate released without an owner")
            self._active = False
            self._dispatch_locked()

    @asynccontextmanager
    async def slot(self, priority: FetchPriority) -> AsyncIterator[None]:
        await self.acquire(priority)
        try:
            yield
        finally:
            await self.release()


class Fetcher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._pw = None
        self._browser = None
        self._lifecycle_lock = asyncio.Lock()
        self._gate = _FairPriorityGate(cfg.cs2_request_min_gap)
        self._closing = False
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._refreshing: set[str] = set()  # background page refresh dedupe
        self._logo_refreshing: set[str] = set()  # background logo fetch dedupe
        self._logo_failed: dict[str, float] = {}  # url -> last-fail epoch (retry-cooldown)

    @property
    def closed(self) -> bool:
        """Whether this instance has completed or started shutdown."""
        return self._closing

    @staticmethod
    def _priority(value: FetchPriority | str) -> FetchPriority:
        if value not in _PRIORITY_RANK:
            allowed = ", ".join(_PRIORITY_RANK)
            raise ValueError(f"unknown fetch priority {value!r}; expected one of {allowed}")
        return value  # type: ignore[return-value]

    @staticmethod
    def _allowed_url(url: str, hosts: frozenset[str]) -> bool:
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").rstrip(".").lower()
            port = parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme.lower() == "https"
            and parsed.username is None
            and parsed.password is None
            and port in (None, 443)
            and host in hosts
        )

    @classmethod
    def _require_allowed_url(cls, url: str, hosts: frozenset[str]) -> None:
        if not cls._allowed_url(url, hosts):
            raise ValueError(f"blocked non-HLTV navigation URL: {url!r}")

    async def start(self) -> None:
        """Start Chromium once; concurrent callers share the same launch."""
        async with self._lifecycle_lock:
            if self._browser:
                return
            if self._closing:
                raise RuntimeError("fetcher is shutting down")

            from playwright.async_api import async_playwright

            pw = await async_playwright().start()
            args = ["--disable-blink-features=AutomationControlled"]
            if self.cfg.cs2_headful:
                # Headful is more reliable for /events.  Keep the window offscreen.
                args += ["--window-position=-32000,-32000", "--window-size=1366,900"]
            try:
                browser = await pw.chromium.launch(
                    headless=not self.cfg.cs2_headful,
                    args=args,
                    ignore_default_args=["--enable-automation"],
                )
            except asyncio.CancelledError:
                await pw.stop()
                raise
            except Exception:
                await pw.stop()
                raise

            self._pw = pw
            self._browser = browser
            logger.info(
                f"[cs2] Chromium 抓取浏览器已启动"
                f"({'有头/屏幕外' if self.cfg.cs2_headful else '无头'})"
            )

    def _track_task(self, coro, *, name: str) -> asyncio.Task[None] | None:
        if self._closing:
            coro.close()
            return None
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def shutdown(self) -> None:
        """Cancel/await all owned background work, then close Playwright."""
        self._closing = True
        tasks = tuple(self._background_tasks)
        try:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await self._gate.close()
            # Browser cleanup still runs if the shutdown coroutine itself is
            # cancelled while it is waiting for its children.
            async with self._lifecycle_lock:
                browser, pw = self._browser, self._pw
                self._browser = self._pw = None
                try:
                    if browser:
                        await browser.close()
                except Exception as exc:  # cleanup failure is useful but non-fatal
                    logger.warning(f"[cs2] Chromium 关闭失败: {exc}")
                finally:
                    if pw:
                        try:
                            await pw.stop()
                        except Exception as exc:
                            logger.warning(f"[cs2] Playwright 关闭失败: {exc}")

    @asynccontextmanager
    async def _navigation_slot(self, priority: FetchPriority) -> AsyncIterator[None]:
        async with self._gate.slot(priority):
            yield

    async def _new_context(self):
        browser = self._browser
        if browser is None:
            raise RuntimeError("fetcher browser is not started")
        return await browser.new_context(
            user_agent=UA,
            locale="en-US",
            viewport={"width": 1366, "height": 900},
        )

    @staticmethod
    async def _close_context(ctx) -> None:
        try:
            await ctx.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Do not let a best-effort browser cleanup error replace the real
            # navigation exception (especially an in-flight cancellation).
            logger.warning(f"[cs2] 浏览器 context 关闭失败: {exc}")

    @staticmethod
    def _is_challenge(title: str) -> bool:
        title = (title or "").lower()
        return "just a moment" in title or "attention required" in title

    @staticmethod
    def _is_error_page(title: str, html: str) -> bool:
        title = (title or "").strip().lower()
        if any(
            marker in title
            for marker in (
                "access denied",
                "service unavailable",
                "bad gateway",
                "gateway timeout",
                "internal server error",
                "page not found",
                "web server is down",
                "origin is unreachable",
                "sorry, you have been blocked",
            )
        ):
            return True
        # All normal HLTV documents currently brand their title.  This also
        # keeps generic proxy/ISP error documents with HTTP 200 out of cache.
        if "hltv.org" not in title:
            return True
        sample = (html or "")[:12000].lower()
        return "sorry, you have been blocked" in sample or (
            "cloudflare ray id" in sample and "access denied" in sample
        )

    async def _guard_top_level_navigation(self, page, hosts: frozenset[str]) -> None:
        """Abort a disallowed main-frame redirect before the request is sent."""

        async def _guard(route, request) -> None:
            if request.is_navigation_request() and request.frame == page.main_frame:
                if not self._allowed_url(request.url, hosts):
                    logger.warning(f"[cs2] 已阻止非 HLTV 重定向: {request.url}")
                    await route.abort("blockedbyclient")
                    return
            await route.continue_()

        await page.route("**/*", _guard)

    async def _navigate_html(
        self,
        page,
        url: str,
        wait_selector: str,
        priority: FetchPriority,
    ) -> Optional[str]:
        """Navigate and return only a verified, non-error HLTV document.

        **挑战重试留在同一个导航档位里**(2026-07-26 实测后改)。冷 context 打 HLTV
        几乎**必吃**一次 403「Just a moment...」(实测 5/5),而一次 ``reload`` 稳定
        1.5~1.7s 放行 —— 这是 Cloudflare 正常流程,不是被封的信号。原来每个 attempt
        各自 ``async with self._navigation_slot(...)``,而闸门在**授权时**就把
        ``_next_grant_at`` 推后了 ``min_gap``,于是那次 reload 要重新排队等满一个
        min_gap(本机 120s):一次抓页实际 ≥120s、且吃掉 **2 个档位**。改成整个重试
        循环共用一个档位后,一次抓页 = 一个档位 ≈ 3s。

        同理不再「等 10s 选择器 + sleep 4s」:挑战页上 ``wait_selector`` 不可能出现
        (实测干等 40s 也不会自己放行),只给一个很短的宽限窗口兜住「挑战自行放行」
        的情况,没放行就立刻 reload。

        ``cs2_fetch_budget_seconds`` 兜住病态情况:HLTV 超时(ERR_TIMED_OUT)时单次
        导航要耗到 ``cs2_nav_timeout``,不加预算的话连续重试会把闸门长占几分钟,
        把用户命令和直播轮询全堵在后面。
        """
        attempts = max(1, self.cfg.cs2_challenge_retries)
        budget = self.cfg.cs2_fetch_budget_seconds
        async with self._navigation_slot(priority):
            started = time.monotonic()
            for attempt in range(attempts):
                try:
                    response = await (
                        page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=self.cfg.cs2_nav_timeout,
                        )
                        if attempt == 0
                        else page.reload(
                            wait_until="domcontentloaded",
                            timeout=self.cfg.cs2_nav_timeout,
                        )
                    )
                    initial_title = await page.title()
                    was_challenge = self._is_challenge(initial_title)
                    selector_found = True
                    try:
                        await page.wait_for_selector(
                            wait_selector,
                            state="attached",
                            timeout=(
                                self.cfg.cs2_challenge_grace_ms if was_challenge else 25000
                            ),
                        )
                    except Exception:  # timeout is validated below, never cached
                        selector_found = False

                    # 还在挑战页(宽限窗口内没自行放行)→ 不必再取 title/content,
                    # 直接进下一轮 reload。
                    if was_challenge and not selector_found:
                        if attempt + 1 < attempts and time.monotonic() - started < budget:
                            continue
                        logger.warning(f"[cs2] Cloudflare 挑战未通过: {url}")
                        return None

                    final_url = page.url
                    if not self._allowed_url(final_url, _PAGE_HOSTS):
                        logger.warning(f"[cs2] 页面重定向到非白名单地址: {final_url}")
                        return None
                    title = await page.title()
                    html = await page.content()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(f"[cs2] 导航失败 {url}: {exc}")
                    return None

                if self._is_challenge(title):
                    if attempt + 1 < attempts and time.monotonic() - started < budget:
                        continue
                    logger.warning(f"[cs2] Cloudflare 挑战未通过: {url}")
                    return None

                # A challenge response may replace itself with a successful document;
                # in that case its initial 403 is no longer the current page status.
                if not was_challenge and (response is None or not response.ok):
                    status = response.status if response else "no response"
                    logger.warning(f"[cs2] HLTV 返回异常状态 {status}: {url}")
                    return None
                if not selector_found:
                    logger.warning(f"[cs2] 页面缺少预期选择器 {wait_selector!r}: {url}")
                    return None
                if self._is_error_page(title, html):
                    logger.warning(f"[cs2] 拒绝缓存错误页 {title!r}: {url}")
                    return None
                return html
        return None

    async def get_html(
        self,
        url: str,
        wait_selector: str = "body",
        max_age: float = 0,
        stale_age: float = 0,
        priority: FetchPriority = PRIORITY_USER,
    ) -> Optional[str]:
        """Fetch an HLTV page, optionally serving a stale cache immediately.

        ``priority`` accepts ``live``, ``user``, ``scan``, or ``warm``.  Existing
        callers default to ``user``.  A stale background refresh inherits the
        caller's priority and is tracked for orderly shutdown.
        """
        self._require_allowed_url(url, _PAGE_HOSTS)
        normalized_priority = self._priority(priority)
        cached = store.cache_get(url, max_age)
        if cached:
            return cached
        if stale_age > max_age:
            stale = store.cache_get(url, stale_age)
            if stale:
                self._spawn_refresh(url, wait_selector, normalized_priority)
                return stale
        return await self._fetch(url, wait_selector, normalized_priority)

    def _spawn_refresh(
        self,
        url: str,
        wait_selector: str,
        priority: FetchPriority,
    ) -> None:
        if self._closing or url in self._refreshing:
            return
        self._refreshing.add(url)

        async def _run() -> None:
            try:
                await self._fetch(url, wait_selector, priority)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[cs2] 后台刷新失败 {url}: {exc}")
            finally:
                self._refreshing.discard(url)

        task = self._track_task(_run(), name="cs2-page-refresh")
        if task is None:
            self._refreshing.discard(url)

    async def _fetch(
        self,
        url: str,
        wait_selector: str,
        priority: FetchPriority,
    ) -> Optional[str]:
        await self.start()
        ctx = await self._new_context()
        try:
            page = await ctx.new_page()
            await self._guard_top_level_navigation(page, _PAGE_HOSTS)
            html = await self._navigate_html(page, url, wait_selector, priority)
            if html is not None:
                # 内存副本同步写(后续 cache_get 立刻命中),1~2MB 的落盘丢线程里做,
                # 别让一次页面写堵住整个 bot(直播轮询、投递重试都在同一个循环上)。
                store.cache_set_mem(url, html)
                await asyncio.to_thread(store.cache_write_disk, url, html)
            return html
        finally:
            await self._close_context(ctx)

    # HLTV typeahead 搜索端点 /search?term= 返回 JSON,直接 goto 会被 Chromium 的 JSON
    # 视图包裹、且冷 context 常吃 Cloudflare 挑战。可靠做法:先在同一 context 落地一个
    # 普通 HLTV 页(清掉 CF),再在页面上下文里 fetch 同源端点拿原始 JSON。
    _SEARCH_JS = (
        "async (term) => {"
        "  const r = await fetch('/search?term=' + encodeURIComponent(term),"
        "    {headers: {'Accept':'application/json','X-Requested-With':'XMLHttpRequest'}});"
        "  return {status: r.status, txt: await r.text()};"
        "}"
    )

    async def fetch_search(
        self, term: str, priority: FetchPriority = PRIORITY_USER
    ) -> Optional[str]:
        """查询 HLTV typeahead 搜索,返回原始 JSON 文本(战队/选手/赛事建议)。

        仅在用户订阅命令里调用(低频)。落地 + 端点 fetch 各占一个节流档。
        """
        term = (term or "").strip()
        if not term:
            return None
        normalized_priority = self._priority(priority)
        await self.start()
        ctx = await self._new_context()
        try:
            page = await ctx.new_page()
            await self._guard_top_level_navigation(page, _PAGE_HOSTS)
            landed = await self._navigate_html(
                page, hltv.URL_MATCHES, ".match", normalized_priority
            )
            if landed is None:
                return None
            async with self._navigation_slot(normalized_priority):
                result = await page.evaluate(self._SEARCH_JS, term)
            if not isinstance(result, dict) or result.get("status") != 200:
                logger.warning(
                    f"[cs2] 搜索失败 term={term!r} status={result.get('status') if isinstance(result, dict) else result}"
                )
                return None
            return result.get("txt")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[cs2] 搜索异常 term={term!r}: {exc}")
            return None
        finally:
            await self._close_context(ctx)

    def spawn_logos(
        self,
        warm_url: str,
        logo_urls: list[str],
        priority: FetchPriority = PRIORITY_WARM,
    ) -> None:
        """Fetch missing logos in a tracked background task.

        Skips URLs that failed within ``cs2_logo_fail_cooldown`` — repeatedly
        re-hitting a batch that's 403-ing (image-CDN Cloudflare block) only
        keeps our IP flagged longer; back off and let the reputation recover.
        """
        self._require_allowed_url(warm_url, _PAGE_HOSTS)
        normalized_priority = self._priority(priority)
        for url in logo_urls:
            if url:
                self._require_allowed_url(url, _ASSET_HOSTS)

        cooldown = self.cfg.cs2_logo_fail_cooldown
        now = time.time()
        fresh = [
            url
            for url in dict.fromkeys(logo_urls)
            if url
            and url not in self._logo_refreshing
            and (not cooldown or now - self._logo_failed.get(url, 0.0) >= cooldown)
        ]
        if self._closing or not fresh:
            return
        self._logo_refreshing.update(fresh)

        async def _run() -> None:
            try:
                await self.get_logos(warm_url, fresh, normalized_priority)  # 内部逐张落盘
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[cs2] 后台 logo 抓取失败: {exc}")
            finally:
                self._logo_refreshing.difference_update(fresh)

        task = self._track_task(_run(), name="cs2-logo-fetch")
        if task is None:
            self._logo_refreshing.difference_update(fresh)

    # Logo pages navigate concurrently (one Playwright page each) after one warm
    # navigation.  They deliberately skip the global min-gap navigation slot
    # (holding it for ~N*2.5s during prewarm used to starve live match polls).
    #
    # NB: fetching logos via a plain (non-browser) GET is NOT a viable shortcut —
    # img-cdn.hltv.org tolerates a couple of unauthenticated requests then 403s
    # bursts (verified 2026-07-19). The browser context carries the CF-clearance
    # cookie from the warm navigation, which is what lets a bulk logo fetch through.
    _LOGO_FETCH_CONCURRENCY = 3

    _LOGO_HASH_RE = re.compile(r"/(teamlogo|eventlogo)/([^/.?&\"']+)")
    _LOGO_URL_RE = re.compile(r"https://img-cdn\.hltv\.org/[^\s\"'<>\\)]+")

    @classmethod
    def _resigned_logo_url(cls, warm_html: str, url: str) -> Optional[str]:
        """用刚拉到的预热页里的同一张图,换掉签名已过期的旧 URL。

        img-cdn 是 imgix,URL 末尾的 ``s=`` 是签名且**会轮换**(实测约小时级)。
        页面缓存/历史记录里的旧 URL 过期后取图恒 403(``text/plain``,与
        Cloudflare 的 ``text/html`` 拦截页不同)。路径哈希不变,故可按
        “同哈希 + 同 day/night 变体(``invert=true`` 与否)”在新页面里找替身;
        ``store.logo_key()`` 也只认哈希,换签名不影响缓存命中。
        """
        m = cls._LOGO_HASH_RE.search(url or "")
        if not (m and warm_html):
            return None
        want_prefix = f"/{m.group(1)}/{m.group(2)}."
        want_invert = "invert=true" in url
        for raw in cls._LOGO_URL_RE.findall(warm_html):
            cand = html_lib.unescape(raw)
            if want_prefix not in cand or cand == url:
                continue
            if ("invert=true" in cand) != want_invert:
                continue
            return cand
        return None

    async def get_logos(
        self,
        warm_url: str,
        logo_urls: list[str],
        priority: FetchPriority = PRIORITY_WARM,
    ) -> dict[str, bytes]:
        """Fetch logo bytes in one CF-warmed context via real page navigations.

        取到的每张图**当场 ``store.save_logo`` 落盘**(调用方无需再存),再一并返回。

        Only the warm HTML page uses the navigation gate. Individual logos are
        navigated on a small pool of pages, so a 20-logo prewarm no longer burns
        ~50s of serialized navigations.

        **坑(2026-07-21 实测)**:不能用 ``ctx.request.get`` 取图。Playwright 的
        APIRequestContext 走的是 Node 自己的 HTTP 栈(TLS/HTTP2 指纹不是
        Chromium),img-cdn.hltv.org 的 Cloudflare 对它**一律 403**——冷/热
        context、补齐 Referer/Sec-Fetch-* 请求头都无用;而同一 context 里对同一
        URL ``page.goto`` 稳定 200。队标必须走真实浏览器导航。
        """
        if not logo_urls:
            return {}
        self._require_allowed_url(warm_url, _PAGE_HOSTS)
        normalized_priority = self._priority(priority)
        unique_urls = [url for url in dict.fromkeys(logo_urls) if url]
        for url in unique_urls:
            self._require_allowed_url(url, _ASSET_HOSTS)
        if not unique_urls:
            return {}

        await self.start()
        out: dict[str, bytes] = {}
        out_lock = asyncio.Lock()
        ctx = await self._new_context()
        try:
            page = await ctx.new_page()
            await self._guard_top_level_navigation(page, _ASSET_HOSTS)
            warm_html = await self._navigate_html(
                page,
                warm_url,
                ".mapholder, body",
                normalized_priority,
            )
            if warm_html is None:
                logger.warning(f"[cs2] logo 预热页验证失败: {warm_url}")
                return out

            pending: asyncio.Queue[str] = asyncio.Queue()
            for url in unique_urls:
                pending.put_nowait(url)

            async def _one(logo_page, url: str) -> None:
                """取一张图;签名过期(403 text/plain)则用预热页里的新签名重试一次。"""
                target = url
                for attempt in (0, 1):
                    try:
                        response = await logo_page.goto(
                            target, timeout=self.cfg.cs2_nav_timeout
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning(f"[cs2] logo 抓取失败 {target}: {exc}")
                        break
                    if response is None:
                        logger.warning(f"[cs2] logo 无响应 {target}")
                        break
                    if not self._allowed_url(logo_page.url, _ASSET_HOSTS):
                        logger.warning(f"[cs2] logo 重定向到非白名单地址: {logo_page.url}")
                        break
                    content_type = (response.headers.get("content-type", "") or "").lower()
                    if response.ok and content_type.startswith("image/"):
                        try:
                            body = await response.body()
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(f"[cs2] logo 读取失败 {target}: {exc}")
                            break
                        if body:
                            # 逐张落盘:一批几十张要几十秒,别等整批跑完才可用,
                            # 也不让关机取消把已抓到的字节全丢掉。
                            store.save_logo(url, body)
                            async with out_lock:
                                out[url] = body
                            self._logo_failed.pop(url, None)  # recovered
                            return
                    # imgix 签名过期返回 text/plain 403;Cloudflare 拦截返回 text/html
                    stale_sig = response.status == 403 and content_type.startswith("text/plain")
                    resigned = (
                        self._resigned_logo_url(warm_html, target)
                        if stale_sig and attempt == 0
                        else None
                    )
                    if resigned:
                        logger.info(f"[cs2] logo 签名过期,改用预热页新链接: {url}")
                        target = resigned
                        continue
                    logger.warning(
                        f"[cs2] logo 响应无效 {target}: "
                        f"status={response.status}, content-type={content_type!r}"
                    )
                    break
                self._logo_failed[url] = time.time()

            async def _worker(logo_page) -> None:
                while True:
                    try:
                        url = pending.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    await _one(logo_page, url)

            # 预热页那个 page 直接复用(它已落地 CF),再按并发上限补几个。
            pages = [page]
            for _ in range(min(self._LOGO_FETCH_CONCURRENCY, len(unique_urls)) - 1):
                extra = await ctx.new_page()
                await self._guard_top_level_navigation(extra, _ASSET_HOSTS)
                pages.append(extra)
            await asyncio.gather(*(_worker(p) for p in pages))
            return out
        finally:
            await self._close_context(ctx)
