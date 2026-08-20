from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

BOT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = BOT_ROOT / "src/plugins/cs2_results"


def _load_fetcher_module() -> ModuleType:
    """Load fetcher.py without importing the side-effectful plugin package."""
    package_name = "cs2_results_fetcher_under_test"
    package = ModuleType(package_name)
    package.__path__ = [str(PLUGIN_DIR)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package

    store = ModuleType(f"{package_name}.store")
    store.cache_get = Mock(return_value=None)  # type: ignore[attr-defined]
    store.cache_set = Mock()  # type: ignore[attr-defined]
    store.save_logo = Mock()  # type: ignore[attr-defined]
    sys.modules[store.__name__] = store

    config = ModuleType(f"{package_name}.config")
    config.Config = object  # type: ignore[attr-defined]
    sys.modules[config.__name__] = config

    name = f"{package_name}.fetcher"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "fetcher.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load fetcher.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _FakeBrowser:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser) -> None:
        self.browser = browser
        self.launch_calls = 0

    async def launch(self, **_kwargs: object) -> _FakeBrowser:
        self.launch_calls += 1
        await asyncio.sleep(0.01)
        return self.browser


class _FakePlaywrightManager:
    def __init__(self) -> None:
        self.browser = _FakeBrowser()
        self.chromium = _FakeChromium(self.browser)
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self) -> _FakePlaywrightManager:
        self.start_calls += 1
        await asyncio.sleep(0)
        return self

    async def stop(self) -> None:
        self.stop_calls += 1


class _FakeResponse:
    def __init__(self, *, ok: bool = True, status: int = 200) -> None:
        self.ok = ok
        self.status = status


class _FakePage:
    def __init__(self, *, title: str, selector_found: bool = True) -> None:
        self.url = "https://www.hltv.org/matches"
        self._title = title
        self._selector_found = selector_found

    async def goto(self, *_args: object, **_kwargs: object) -> _FakeResponse:
        return _FakeResponse()

    async def reload(self, *_args: object, **_kwargs: object) -> _FakeResponse:
        return _FakeResponse()

    async def title(self) -> str:
        return self._title

    async def wait_for_selector(self, *_args: object, **_kwargs: object) -> object:
        if not self._selector_found:
            raise TimeoutError("selector missing")
        return object()

    async def content(self) -> str:
        return "<html><body><div class='match'></div></body></html>"


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self.page = page
        self.closed = False

    async def new_page(self) -> _FakePage:
        return self.page

    async def close(self) -> None:
        self.closed = True


class FetcherUrlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fetcher_module = _load_fetcher_module()

    def test_navigation_url_allowlists(self) -> None:
        fetcher = self.fetcher_module.Fetcher
        page_hosts = self.fetcher_module._PAGE_HOSTS
        asset_hosts = self.fetcher_module._ASSET_HOSTS

        self.assertTrue(fetcher._allowed_url("https://www.hltv.org/matches", page_hosts))
        self.assertTrue(fetcher._allowed_url("https://hltv.org:443/events", page_hosts))
        self.assertTrue(
            fetcher._allowed_url("https://img-cdn.hltv.org/teamlogo/a.png", asset_hosts)
        )
        self.assertFalse(
            fetcher._allowed_url("https://img-cdn.hltv.org/teamlogo/a.png", page_hosts)
        )

        rejected = (
            "http://www.hltv.org/matches",
            "file:///etc/passwd",
            "https://localhost/matches",
            "https://127.0.0.1/matches",
            "https://192.168.1.8/matches",
            "https://user@www.hltv.org/matches",
            "https://www.hltv.org:444/matches",
            "https://www.hltv.org.evil.test/matches",
            "https://evil.hltv.org/matches",
        )
        for url in rejected:
            with self.subTest(url=url):
                self.assertFalse(fetcher._allowed_url(url, page_hosts))


class FetcherAsyncTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fetcher_module = _load_fetcher_module()

    @staticmethod
    def _config(*, min_gap: float = 0.0) -> SimpleNamespace:
        return SimpleNamespace(
            cs2_request_min_gap=min_gap,
            cs2_headful=False,
            cs2_challenge_retries=1,
            cs2_nav_timeout=100,
        )

    async def test_concurrent_start_launches_browser_once(self) -> None:
        manager = _FakePlaywrightManager()
        playwright_package = ModuleType("playwright")
        playwright_package.__path__ = []  # type: ignore[attr-defined]
        playwright_api = ModuleType("playwright.async_api")
        playwright_api.async_playwright = lambda: manager  # type: ignore[attr-defined]
        playwright_package.async_api = playwright_api  # type: ignore[attr-defined]

        fetcher = self.fetcher_module.Fetcher(self._config())
        with patch.dict(
            sys.modules,
            {"playwright": playwright_package, "playwright.async_api": playwright_api},
        ):
            await asyncio.gather(*(fetcher.start() for _ in range(20)))

        self.assertEqual(manager.start_calls, 1)
        self.assertEqual(manager.chromium.launch_calls, 1)
        await fetcher.shutdown()
        self.assertEqual(manager.browser.close_calls, 1)
        self.assertEqual(manager.stop_calls, 1)

    async def test_live_wins_at_gap_deadline_even_if_warm_queued_first(self) -> None:
        gate = self.fetcher_module._FairPriorityGate(min_gap=0.04)
        order: list[str] = []

        async with gate.slot("scan"):
            pass  # establish a future min-gap deadline

        async def enter(priority: str) -> None:
            async with gate.slot(priority):
                order.append(priority)

        warm = asyncio.create_task(enter("warm"))
        await asyncio.sleep(0.005)
        live = asyncio.create_task(enter("live"))
        await asyncio.gather(warm, live)
        await gate.close()

        self.assertEqual(order, ["live", "warm"])

    async def test_cancelling_queued_waiter_does_not_wedge_gate(self) -> None:
        gate = self.fetcher_module._FairPriorityGate()
        await gate.acquire("live")
        queued = asyncio.create_task(gate.acquire("warm"))
        await asyncio.sleep(0)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued

        await gate.release()
        async with gate.slot("scan"):
            pass
        await gate.close()

    async def test_shutdown_cancels_and_awaits_owned_background_tasks(self) -> None:
        fetcher = self.fetcher_module.Fetcher(self._config())
        started = asyncio.Event()

        async def background() -> None:
            started.set()
            await asyncio.Future()

        task = fetcher._track_task(background(), name="fetcher-test-background")
        self.assertIsNotNone(task)
        await started.wait()
        await fetcher.shutdown()

        assert task is not None
        self.assertTrue(task.done())
        self.assertTrue(task.cancelled())
        self.assertFalse(fetcher._background_tasks)

    async def test_error_or_missing_selector_is_never_cached(self) -> None:
        cases = (
            _FakePage(title="Bad Gateway", selector_found=True),
            _FakePage(title="Matches | HLTV.org", selector_found=False),
        )
        url = "https://www.hltv.org/matches"

        for page in cases:
            with self.subTest(title=page._title, selector_found=page._selector_found):
                context = _FakeContext(page)
                fetcher = self.fetcher_module.Fetcher(self._config())
                fetcher.start = AsyncMock()  # type: ignore[method-assign]
                fetcher._new_context = AsyncMock(return_value=context)  # type: ignore[method-assign]
                fetcher._guard_top_level_navigation = AsyncMock()  # type: ignore[method-assign]
                cache_set = Mock()

                with patch.object(self.fetcher_module.store, "cache_set", cache_set):
                    html = await fetcher._fetch(url, ".match", "user")

                self.assertIsNone(html)
                cache_set.assert_not_called()
                self.assertTrue(context.closed)
                await fetcher.shutdown()

    async def test_get_logos_uses_request_api_without_per_logo_navigation_slot(
        self,
    ) -> None:
        """Logos share one warm navigation; assets go through request.get concurrently."""
        warm_url = "https://www.hltv.org/matches"
        logo_a = "https://img-cdn.hltv.org/teamlogo/a.png"
        logo_b = "https://img-cdn.hltv.org/teamlogo/b.png"

        class _LogoResponse:
            def __init__(self, url: str, body: bytes) -> None:
                self.url = url
                self.ok = True
                self.status = 200
                self.headers = {"content-type": "image/png"}
                self._body = body

            async def body(self) -> bytes:
                return self._body

        class _FakeRequest:
            def __init__(self) -> None:
                self.urls: list[str] = []

            async def get(self, url: str, **_kwargs: object) -> _LogoResponse:
                self.urls.append(url)
                return _LogoResponse(url, b"png-" + url.encode()[-6:])

        class _LogoContext(_FakeContext):
            def __init__(self, page: _FakePage) -> None:
                super().__init__(page)
                self.request = _FakeRequest()

        page = _FakePage(title="Matches | HLTV.org")
        context = _LogoContext(page)
        fetcher = self.fetcher_module.Fetcher(self._config(min_gap=0.05))
        fetcher.start = AsyncMock()  # type: ignore[method-assign]
        fetcher._new_context = AsyncMock(return_value=context)  # type: ignore[method-assign]
        fetcher._guard_top_level_navigation = AsyncMock()  # type: ignore[method-assign]

        slot_enters = 0
        real_slot = fetcher._navigation_slot

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def counting_slot(priority):  # type: ignore[no-untyped-def]
            nonlocal slot_enters
            slot_enters += 1
            async with real_slot(priority):
                yield

        fetcher._navigation_slot = counting_slot  # type: ignore[method-assign]

        got = await fetcher.get_logos(warm_url, [logo_a, logo_b], priority="warm")

        self.assertEqual(set(got), {logo_a, logo_b})
        self.assertEqual(set(context.request.urls), {logo_a, logo_b})
        # Only the warm HTML page should take a navigation slot — not each logo.
        self.assertEqual(slot_enters, 1)
        self.assertTrue(context.closed)
        await fetcher.shutdown()


if __name__ == "__main__":
    unittest.main()
