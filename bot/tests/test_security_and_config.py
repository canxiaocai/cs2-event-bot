from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType

from pydantic import ValidationError

BOT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = BOT_ROOT / "src/plugins/cs2_results"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_security_module() -> ModuleType:
    """Load security.py with its relative hltv import, without package startup."""
    package_name = "cs2_results_security_under_test"
    package = ModuleType(package_name)
    package.__path__ = [str(PLUGIN_DIR)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package
    _load_module(f"{package_name}.hltv", PLUGIN_DIR / "hltv.py")
    return _load_module(f"{package_name}.security", PLUGIN_DIR / "security.py")


class HltvMatchUrlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.security = _load_security_module()

    def test_accepts_numeric_match_id(self) -> None:
        self.assertEqual(
            self.security.hltv_match_url(" 12345 "),
            "https://www.hltv.org/matches/12345/x",
        )

    def test_accepts_canonical_https_url_and_removes_query_and_fragment(self) -> None:
        accepted = {
            "https://www.hltv.org/matches/12345/alpha-vs-beta": (
                "https://www.hltv.org/matches/12345/alpha-vs-beta"
            ),
            " https://www.hltv.org/matches/12345/alpha-vs-beta?utm=x#stats ": (
                "https://www.hltv.org/matches/12345/alpha-vs-beta"
            ),
            "https://www.hltv.org:443/matches/12345/": ("https://www.hltv.org/matches/12345/"),
        }
        for raw, expected in accepted.items():
            with self.subTest(raw=raw):
                self.assertEqual(self.security.hltv_match_url(raw), expected)

    def test_rejects_non_hltv_or_unsafe_urls(self) -> None:
        rejected = (
            "http://www.hltv.org/matches/123/a",
            "https://user@www.hltv.org/matches/123/a",
            "https://user:pass@www.hltv.org/matches/123/a",
            "https://www.hltv.org:444/matches/123/a",
            "https://www.hltv.org:notaport/matches/123/a",
            "https://localhost/matches/123/a",
            "https://127.0.0.1/matches/123/a",
            "https://192.168.1.10/matches/123/a",
            "https://www.hltv.org.evil.test/matches/123/a",
            "https://evil.test/matches/123/a",
            "https://www.hltv.org/events/123/a",
            "https://www.hltv.org/matches/not-a-number/a",
            "１２３４５",
            "https://www.hl\ntv.org/matches/123/a",
            "https://www.hltv.org/matches/１２３/a",
            "javascript:alert(1)",
            "not a url",
            "",
        )
        for raw in rejected:
            with self.subTest(raw=raw):
                self.assertIsNone(self.security.hltv_match_url(raw))


class ConfigBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.Config = _load_module("cs2_config_under_test", PLUGIN_DIR / "config.py").Config

    def test_inclusive_boundaries_are_accepted(self) -> None:
        config = self.Config(
            cs2_live_poll_interval=1,
            cs2_featured_refresh_hour=0,
            cs2_cache_cleanup_hour=23,
            cs2_featured_sticky_days=0,
            cs2_cache_matches_ttl=0,
            cs2_stale_matches=0,
            cs2_request_min_gap=0,
            cs2_command_cooldown=0,
        )

        self.assertEqual(config.cs2_live_poll_interval, 1)
        self.assertEqual(config.cs2_featured_refresh_hour, 0)
        self.assertEqual(config.cs2_cache_cleanup_hour, 23)
        self.assertEqual(config.cs2_request_min_gap, 0)

    def test_numeric_fields_reject_values_outside_their_boundaries(self) -> None:
        invalid_cases = (
            {"cs2_live_poll_interval": 0},
            {"cs2_matches_scan_interval": 0},
            {"cs2_max_followed": 0},
            {"cs2_featured_refresh_hour": -1},
            {"cs2_featured_refresh_hour": 24},
            {"cs2_cache_cleanup_hour": 24},
            {"cs2_request_min_gap": -0.1},
            {"cs2_nav_timeout": 0},
            {"cs2_nav_timeout": 120001},
            {"cs2_challenge_retries": 0},
            {"cs2_challenge_retries": 11},
            {"cs2_command_cooldown": -0.1},
            {"cs2_delivery_max_attempts": 0},
            {"cs2_delivery_retry_base_seconds": 0},
            {"cs2_scan_cache_max_age": -1},
            {"cs2_stuck_follow_hours": 0.5},
            {"cs2_startup_backstop_window_min": 0},
        )
        for values in invalid_cases:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.Config(**values)

    def test_startup_backstop_window_not_shorter_than_regular(self) -> None:
        config = self.Config(
            cs2_backstop_window_min=120,
            cs2_startup_backstop_window_min=720,
        )
        self.assertEqual(config.cs2_startup_backstop_window_min, 720)
        with self.assertRaises(ValidationError):
            self.Config(
                cs2_backstop_window_min=200,
                cs2_startup_backstop_window_min=100,
            )

    def test_stale_window_is_zero_or_at_least_fresh_ttl(self) -> None:
        pairs = (
            ("cs2_cache_matches_ttl", "cs2_stale_matches"),
            ("cs2_cache_results_ttl", "cs2_stale_results"),
            ("cs2_cache_events_ttl", "cs2_stale_events"),
            ("cs2_cache_event_page_ttl", "cs2_stale_event_page"),
        )
        for fresh_name, stale_name in pairs:
            with self.subTest(pair=(fresh_name, stale_name), case="disabled"):
                config = self.Config(**{fresh_name: 10, stale_name: 0})
                self.assertEqual(getattr(config, stale_name), 0)
            with self.subTest(pair=(fresh_name, stale_name), case="equal"):
                config = self.Config(**{fresh_name: 10, stale_name: 10})
                self.assertEqual(getattr(config, stale_name), 10)
            with self.subTest(pair=(fresh_name, stale_name), case="too_short"):
                with self.assertRaises(ValidationError):
                    self.Config(**{fresh_name: 10, stale_name: 9})

    def test_identifiers_are_positive_ascii_and_deduplicated(self) -> None:
        config = self.Config(
            cs2_subscribed_groups=[123, 123, 456],
            cs2_debug_groups=[456, 456],
            cs2_force_include_events=["123", "123", "456"],
        )
        self.assertEqual(config.cs2_subscribed_groups, [123, 456])
        self.assertEqual(config.cs2_debug_groups, [456])
        self.assertEqual(config.cs2_force_include_events, ["123", "456"])
        for values in (
            {"cs2_subscribed_groups": [0]},
            {"cs2_debug_groups": [-1]},
            {"cs2_force_include_events": ["１２３"]},
            {"cs2_force_exclude_events": ["abc"]},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.Config(**values)


if __name__ == "__main__":
    unittest.main()
