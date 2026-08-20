from __future__ import annotations

import asyncio
import importlib.util
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

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


def _load_delivery_module() -> tuple[ModuleType, type[Any]]:
    """Load delivery.py without importing cs2_results/__init__.py or its real store."""
    package_name = "cs2_delivery_under_test"
    package = ModuleType(package_name)
    package.__path__ = [str(PLUGIN_DIR)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package

    config = _load_module(f"{package_name}.config", PLUGIN_DIR / "config.py")
    sys.modules[f"{package_name}.store"] = ModuleType(f"{package_name}.store")
    delivery = _load_module(f"{package_name}.delivery", PLUGIN_DIR / "delivery.py")
    return delivery, config.Config


delivery, Config = _load_delivery_module()


@dataclass(frozen=True, slots=True)
class FakeDelivery:
    match_id: str
    map_key: str
    group_id: int
    attempts: int = 0
    status: str = "pending"


class FakeStore:
    def __init__(
        self,
        claimed: list[FakeDelivery],
        *,
        active_groups: set[int] | None = None,
        payloads: dict[tuple[str, str], bytes | None] | None = None,
        failure_statuses: dict[int, str] | None = None,
        released: int = 0,
    ) -> None:
        self.claimed = claimed
        self.active_groups = active_groups or set()
        self.payloads = payloads or {}
        self.failure_statuses = failure_statuses or {}
        self.released = released

        self.claim_calls: list[tuple[str, int, int]] = []
        self.payload_calls: list[tuple[str, str]] = []
        self.sent_calls: list[tuple[str, str, int, str]] = []
        self.failed_calls: list[dict[str, Any]] = []
        self.defer_calls: list[dict[str, Any]] = []
        self.release_calls: list[str] = []
        self.release_claim_calls: list[tuple[str, str, int, str]] = []
        self.unsubscribe_calls: list[int] = []

    def claim_due_deliveries(
        self,
        worker_id: str,
        *,
        lease_seconds: int,
        limit: int,
    ) -> list[FakeDelivery]:
        self.claim_calls.append((worker_id, lease_seconds, limit))
        return self.claimed[:limit]

    def get_subscriptions(self) -> set[int]:
        return set(self.active_groups)

    def get_delivery_payload(self, match_id: str, map_key: str) -> bytes | None:
        key = (match_id, map_key)
        self.payload_calls.append(key)
        return self.payloads.get(key)

    def mark_delivery_sent(
        self,
        match_id: str,
        map_key: str,
        group_id: int,
        *,
        worker_id: str,
    ) -> FakeDelivery | None:
        self.sent_calls.append((match_id, map_key, group_id, worker_id))
        current = self._find(match_id, map_key, group_id)
        return replace(current, attempts=current.attempts + 1, status="sent")

    def mark_delivery_failed(
        self,
        match_id: str,
        map_key: str,
        group_id: int,
        error: str,
        *,
        next_retry_at: float | None = None,
        max_attempts: int | None = None,
        dead: bool = False,
        worker_id: str,
    ) -> FakeDelivery:
        self.failed_calls.append(
            {
                "match_id": match_id,
                "map_key": map_key,
                "group_id": group_id,
                "error": error,
                "next_retry_at": next_retry_at,
                "max_attempts": max_attempts,
                "dead": dead,
                "worker_id": worker_id,
            }
        )
        current = self._find(match_id, map_key, group_id)
        if dead:
            status = "dead"
        else:
            status = self.failure_statuses.get(group_id, "retry")
        return replace(current, attempts=current.attempts + 1, status=status)

    def defer_delivery(
        self,
        match_id: str,
        map_key: str,
        group_id: int,
        retry_at: float,
        error: str,
        *,
        worker_id: str,
    ) -> FakeDelivery:
        self.defer_calls.append(
            {
                "match_id": match_id,
                "map_key": map_key,
                "group_id": group_id,
                "retry_at": retry_at,
                "error": error,
                "worker_id": worker_id,
            }
        )
        # Deferral deliberately preserves attempts.
        return self._find(match_id, map_key, group_id)

    def release_claim(
        self,
        match_id: str,
        map_key: str,
        group_id: int,
        *,
        worker_id: str,
    ) -> bool:
        self.release_claim_calls.append((match_id, map_key, group_id, worker_id))
        return True

    def release_claims(self, worker_id: str) -> int:
        self.release_calls.append(worker_id)
        return self.released

    def unsubscribe(self, group_id: int) -> bool:
        self.unsubscribe_calls.append(group_id)
        if group_id in self.active_groups:
            self.active_groups.discard(group_id)
            return True
        return False

    def _find(self, match_id: str, map_key: str, group_id: int) -> FakeDelivery:
        return next(
            item
            for item in self.claimed
            if (item.match_id, item.map_key, item.group_id) == (match_id, map_key, group_id)
        )


class FakeBot:
    def __init__(self, failures: dict[int, Exception] | None = None) -> None:
        self.failures = failures or {}
        self.calls: list[tuple[int, Any]] = []

    async def send_group_msg(self, *, group_id: int, message: Any) -> None:
        self.calls.append((group_id, message))
        if error := self.failures.get(group_id):
            raise error


def _worker(fake_store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(delivery, "store", fake_store)
    monkeypatch.setattr(delivery, "time", SimpleNamespace(time=lambda: 1_000.0))
    config = Config(
        cs2_delivery_max_attempts=3,
        cs2_delivery_retry_base_seconds=10,
    )
    return delivery.DeliveryWorker(config)


def test_success_marks_each_delivery_sent_and_reuses_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = [
        FakeDelivery("match-1", "map-1", 1001),
        FakeDelivery("match-1", "map-1", 1002),
    ]
    fake_store = FakeStore(
        claimed,
        active_groups={1001, 1002},
        payloads={("match-1", "map-1"): b"png payload"},
    )
    bot = FakeBot()
    monkeypatch.setattr(delivery, "get_bot", lambda: bot)
    worker = _worker(fake_store, monkeypatch)

    result = asyncio.run(worker.run_once(limit=20))

    assert result == delivery.DeliveryRun(claimed=2, sent=2)
    assert [group_id for group_id, _ in bot.calls] == [1001, 1002]
    assert fake_store.payload_calls == [("match-1", "map-1")]
    assert [call[2] for call in fake_store.sent_calls] == [1001, 1002]
    assert bot.calls[0][1] is bot.calls[1][1]


def test_partial_send_failures_become_retry_and_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = [
        FakeDelivery("match-2", "map-2", 2001),
        FakeDelivery("match-2", "map-2", 2002, attempts=0),
        FakeDelivery("match-2", "map-2", 2003, attempts=2),
    ]
    fake_store = FakeStore(
        claimed,
        active_groups={2001, 2002, 2003},
        payloads={("match-2", "map-2"): b"png payload"},
        failure_statuses={2002: "retry", 2003: "dead"},
    )
    bot = FakeBot(
        {
            2002: RuntimeError("temporary send failure"),
            2003: RuntimeError("final send failure"),
        }
    )
    monkeypatch.setattr(delivery, "get_bot", lambda: bot)
    worker = _worker(fake_store, monkeypatch)

    result = asyncio.run(worker.run_once())

    assert result == delivery.DeliveryRun(claimed=3, sent=1, retried=1, dead=1)
    assert [call[2] for call in fake_store.sent_calls] == [2001]
    failures = {call["group_id"]: call for call in fake_store.failed_calls}
    assert failures[2002]["next_retry_at"] == 1_010.0
    assert failures[2003]["next_retry_at"] == 1_040.0
    assert all(call["max_attempts"] == 3 for call in failures.values())


def test_bot_unavailable_defers_without_consuming_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = [
        FakeDelivery("match-3", "map-3", 3001, attempts=2),
        FakeDelivery("match-3", "map-3", 3002, attempts=1),
    ]
    fake_store = FakeStore(claimed, active_groups={3001, 3002})

    def unavailable_bot() -> None:
        raise RuntimeError("OneBot disconnected")

    monkeypatch.setattr(delivery, "get_bot", unavailable_bot)
    worker = _worker(fake_store, monkeypatch)

    result = asyncio.run(worker.run_once())

    assert result == delivery.DeliveryRun(claimed=2, deferred=2)
    assert fake_store.failed_calls == []
    assert fake_store.sent_calls == []
    assert [call["retry_at"] for call in fake_store.defer_calls] == [1_010.0, 1_010.0]
    assert [item.attempts for item in claimed] == [2, 1]


def test_missing_payload_defers_without_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = [FakeDelivery("match-4", "map-4", 4001, attempts=2)]
    fake_store = FakeStore(
        claimed,
        active_groups={4001},
        payloads={("match-4", "map-4"): None},
    )
    bot = FakeBot()
    monkeypatch.setattr(delivery, "get_bot", lambda: bot)
    worker = _worker(fake_store, monkeypatch)

    result = asyncio.run(worker.run_once())

    assert result == delivery.DeliveryRun(claimed=1, deferred=1)
    assert bot.calls == []
    assert fake_store.failed_calls == []
    assert fake_store.defer_calls[0]["retry_at"] == 1_060.0
    assert fake_store.defer_calls[0]["error"] == "outbox payload missing"
    assert claimed[0].attempts == 2


def test_unsubscribed_claim_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = [
        FakeDelivery("match-5", "map-5", 5001),
        FakeDelivery("match-5", "map-5", 5002),
    ]
    fake_store = FakeStore(
        claimed,
        active_groups={5002},
        payloads={("match-5", "map-5"): b"png payload"},
    )
    bot = FakeBot()
    monkeypatch.setattr(delivery, "get_bot", lambda: bot)
    worker = _worker(fake_store, monkeypatch)

    result = asyncio.run(worker.run_once())

    assert result == delivery.DeliveryRun(claimed=2, sent=1, released=1)
    assert [group_id for group_id, _ in bot.calls] == [5002]
    assert [call[2] for call in fake_store.sent_calls] == [5002]
    assert fake_store.failed_calls == []
    assert fake_store.defer_calls == []
    assert fake_store.release_claim_calls == [
        ("match-5", "map-5", 5001, worker._worker_id)
    ]


def test_permanent_group_already_unsubscribed_releases_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Permanent send failure when group is already gone must not hold the lease."""
    claimed = [FakeDelivery("match-gone2", "map-1", 9001, attempts=0)]
    fake_store = FakeStore(
        claimed,
        active_groups={9001},
        payloads={("match-gone2", "map-1"): b"png payload"},
    )
    # First unsubscribe succeeds; second permanent failure on same group returns False
    # if we only had one delivery — simulate already-unsubscribed by making unsubscribe
    # a no-op (group not listed).
    fake_store.active_groups = {9001}

    unsub_results = iter([False])  # already removed from list elsewhere

    def _unsub(group_id: int) -> bool:
        fake_store.unsubscribe_calls.append(group_id)
        fake_store.active_groups.discard(group_id)
        return next(unsub_results, False)

    fake_store.unsubscribe = _unsub  # type: ignore[method-assign]
    bot = FakeBot({9001: RuntimeError("不在群内")})
    monkeypatch.setattr(delivery, "get_bot", lambda: bot)
    worker = _worker(fake_store, monkeypatch)

    result = asyncio.run(worker.run_once())

    assert result.unsubscribed == 0
    assert result.sent == 0
    assert fake_store.release_claim_calls == [
        ("match-gone2", "map-1", 9001, worker._worker_id)
    ]


def test_release_claims_uses_worker_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_store = FakeStore([], released=4)
    worker = _worker(fake_store, monkeypatch)

    released = worker.release_claims()

    assert released == 4
    assert fake_store.release_calls == [worker._worker_id]


def test_classify_send_failure_group_blocks() -> None:
    mute_err = RuntimeError(
        "ActionFailed(status='failed', retcode=1200, message='EventChecker Failed: "
        'NTEvent ... EventRet:\\n{\\n    "result": 120,\\n    "errMsg": ""\\n}\\n\')'
    )
    assert delivery.classify_send_failure(mute_err) == "temporary_group"
    assert delivery.classify_send_failure(RuntimeError("全员禁言中")) == "temporary_group"
    assert delivery.classify_send_failure(RuntimeError("机器人已被禁言")) == "temporary_group"
    assert delivery.classify_send_failure(RuntimeError("不在群内")) == "permanent_group"
    assert delivery.classify_send_failure(RuntimeError("群已解散")) == "permanent_group"
    assert (
        delivery.classify_send_failure(RuntimeError('rich media transfer failed "result": -1'))
        == "transient"
    )


def test_group_mute_dead_does_not_count_as_alertable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mute/ban-style failures become dead_expected, not dead (no admin alert)."""
    claimed = [FakeDelivery("match-mute", "map-1", 6001, attempts=2)]
    fake_store = FakeStore(
        claimed,
        active_groups={6001},
        payloads={("match-mute", "map-1"): b"png payload"},
        failure_statuses={6001: "dead"},
    )
    bot = FakeBot(
        {
            6001: RuntimeError(
                "ActionFailed(status='failed', retcode=1200, data=None, "
                'message=\'EventChecker Failed: EventRet:\\n{\\n    "result": 120,\\n'
                '    "errMsg": ""\\n}\\n\')'
            )
        }
    )
    monkeypatch.setattr(delivery, "get_bot", lambda: bot)
    worker = _worker(fake_store, monkeypatch)

    result = asyncio.run(worker.run_once())

    assert result == delivery.DeliveryRun(claimed=1, dead_expected=1)
    assert result.dead == 0
    assert fake_store.failed_calls[0]["dead"] is False  # still went through max_attempts path


def test_permanent_group_failure_auto_unsubscribes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = [
        FakeDelivery("match-gone", "map-1", 7001, attempts=0),
        FakeDelivery("match-gone", "map-2", 7001, attempts=0),
        FakeDelivery("match-ok", "map-1", 7002, attempts=0),
    ]
    fake_store = FakeStore(
        claimed,
        active_groups={7001, 7002},
        payloads={
            ("match-gone", "map-1"): b"png payload",
            ("match-gone", "map-2"): b"png payload 2",
            ("match-ok", "map-1"): b"png other",
        },
    )
    bot = FakeBot({7001: RuntimeError("不在群内")})
    monkeypatch.setattr(delivery, "get_bot", lambda: bot)
    worker = _worker(fake_store, monkeypatch)

    result = asyncio.run(worker.run_once())

    # Second delivery for 7001 is skipped after the first permanent failure unsubscribes
    # the group; its lease is released immediately (released=1) instead of waiting 300s.
    assert result == delivery.DeliveryRun(claimed=3, sent=1, unsubscribed=1, released=1)
    assert result.dead == 0
    assert result.dead_expected == 0
    assert fake_store.unsubscribe_calls == [7001]
    assert 7001 not in fake_store.active_groups
    assert fake_store.failed_calls == []
    assert [call[2] for call in fake_store.sent_calls] == [7002]
    assert fake_store.release_claim_calls == [
        ("match-gone", "map-2", 7001, worker._worker_id)
    ]


def test_drop_unreachable_subscription_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_store = FakeStore([], active_groups={8001})
    monkeypatch.setattr(delivery, "store", fake_store)

    assert delivery.drop_unreachable_subscription(8001, reason="群已解散") is True
    assert delivery.drop_unreachable_subscription(8001, reason="群已解散") is False
    assert fake_store.unsubscribe_calls == [8001, 8001]
    assert 8001 not in fake_store.active_groups
