from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import shutil
import sqlite3
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
STORE_SOURCE = BOT_DIR / "src/plugins/cs2_results/store.py"


def _create_v1_database(data_dir: Path) -> None:
    with sqlite3.connect(data_dir / "state.sqlite3") as conn:
        conn.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata VALUES ('schema_version', '1');
            CREATE TABLE subscriptions (
                group_id INTEGER PRIMARY KEY,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            INSERT INTO subscriptions VALUES (4242, 10, 10);
            CREATE TABLE delivery_batches (
                match_id TEXT NOT NULL,
                map_key TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (match_id, map_key)
            );
            INSERT INTO delivery_batches VALUES ('legacy-match', 'legacy-map', 10);
            CREATE TABLE deliveries (
                match_id TEXT NOT NULL,
                map_key TEXT NOT NULL,
                group_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'sent', 'retry', 'dead')),
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                next_retry_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                sent_at REAL,
                PRIMARY KEY (match_id, map_key, group_id),
                FOREIGN KEY (match_id, map_key)
                    REFERENCES delivery_batches(match_id, map_key) ON DELETE CASCADE
            );
            INSERT INTO deliveries VALUES (
                'legacy-match', 'legacy-map', 4242, 'retry', 3,
                'legacy error', 123, 10, 11, NULL
            );
            CREATE INDEX idx_deliveries_due
                ON deliveries(match_id, map_key, status, next_retry_at);
            """
        )


def _create_future_database(data_dir: Path) -> None:
    with sqlite3.connect(data_dir / "state.sqlite3") as conn:
        conn.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata VALUES ('schema_version', '3');
            """
        )


@pytest.fixture
def store_factory(tmp_path: Path) -> Iterator[Callable[..., Any]]:
    """Load store.py with __file__ rooted under a temporary bot directory."""
    loaded_names: list[str] = []

    def load(
        legacy_subscriptions: list[int],
        prepare_data: Callable[[Path], None] | None = None,
    ) -> Any:
        root = tmp_path / f"case-{len(loaded_names)}"
        plugin_dir = root / "bot/src/plugins/cs2_results"
        plugin_dir.mkdir(parents=True)
        fake_store_path = plugin_dir / "store.py"
        shutil.copy2(STORE_SOURCE, fake_store_path)

        data_dir = root / "bot/data/cs2_results"
        data_dir.mkdir(parents=True)
        (data_dir / "subscriptions.json").write_text(
            json.dumps(legacy_subscriptions), encoding="utf-8"
        )
        if prepare_data is not None:
            prepare_data(data_dir)

        name = f"cs2_store_under_test_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(name, fake_store_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        loaded_names.append(name)
        spec.loader.exec_module(module)

        # This is the isolation contract: every derived persistence path must
        # live below tmp_path, never the repository's real bot/data directory.
        assert module.DATA_DIR == data_dir
        assert module._DB == data_dir / "state.sqlite3"
        assert module._DB.is_relative_to(tmp_path)
        return module

    yield load

    for name in loaded_names:
        sys.modules.pop(name, None)


def test_legacy_subscription_migration_is_idempotent(
    store_factory: Callable[..., Any],
) -> None:
    store = store_factory([1001, 1002, 1002])

    assert store.schema_version() == 2
    assert store.get_subscriptions() == {1001, 1002}
    assert store.subscription_count() == 2

    # Changing the legacy file after the committed marker must not replay it.
    store._SUBS.write_text("[9999]", encoding="utf-8")
    assert store.migrate_legacy_subscriptions() == 0
    assert store.get_subscriptions() == {1001, 1002}


def test_schema_v1_migrates_to_v2_without_losing_state(
    store_factory: Callable[..., Any],
) -> None:
    store = store_factory([], _create_v1_database)

    assert store.schema_version() == 2
    assert store.get_subscriptions() == {4242}
    delivery = store.get_delivery("legacy-match", "legacy-map", 4242)
    assert delivery.status == "retry"
    assert delivery.attempts == 3
    assert delivery.last_error == "legacy error"
    assert delivery.cancelled_at is None
    assert delivery.claim_owner is None
    batch = store.get_delivery_batch("legacy-match", "legacy-map")
    assert batch.created_at == 10
    assert batch.updated_at == 10
    assert batch.payload_path is None


def test_future_schema_is_rejected_without_downgrade(
    store_factory: Callable[..., Any], tmp_path: Path
) -> None:
    with pytest.raises(RuntimeError, match="状态库版本为 3"):
        store_factory([], _create_future_database)

    databases = list(tmp_path.glob("case-*/bot/data/cs2_results/state.sqlite3"))
    assert len(databases) == 1
    with sqlite3.connect(databases[0]) as conn:
        version = conn.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()[0]
    assert version == "3"


def test_config_seed_once_does_not_resurrect_unsubscribed_group(
    store_factory: Callable[..., Any],
) -> None:
    store = store_factory([])

    assert store.seed_subscriptions_once([3001]) == 1
    assert store.unsubscribe(3001) is True
    assert store.seed_subscriptions_once([3001]) == 0
    assert 3001 not in store.get_subscriptions()

    # An empty initial config is still a completed migration.
    assert store.seed_subscriptions_once([], marker="empty-config-v1") == 0
    assert store.seed_subscriptions_once([3002], marker="empty-config-v1") == 0
    assert 3002 not in store.get_subscriptions()


def test_outbox_freezes_recipients_and_tracks_retry_to_dead(
    store_factory: Callable[..., Any],
) -> None:
    store = store_factory([])

    prepared = store.prepare_deliveries("match-1", "map-1", [1001, 1002])
    assert [delivery.group_id for delivery in prepared] == [1001, 1002]
    assert {delivery.status for delivery in prepared} == {"pending"}

    # Later subscriptions do not get appended to an already frozen map batch.
    frozen = store.prepare_deliveries("match-1", "map-1", [9999])
    assert [delivery.group_id for delivery in frozen] == [1001, 1002]
    assert store.delivery_complete("match-1", "map-1") is False
    assert store.delivery_all_sent("match-1", "map-1") is False

    sent = store.mark_delivery_sent("match-1", "map-1", 1001)
    assert sent.status == "sent"
    assert sent.attempts == 1
    assert store.mark_delivery_sent("match-1", "map-1", 1001) is None

    retry = store.mark_delivery_failed(
        "match-1",
        "map-1",
        1002,
        "network unavailable",
        next_retry_at=500.0,
    )
    assert retry.status == "retry"
    assert retry.attempts == 1
    assert store.due_deliveries("match-1", "map-1", now=499.9) == []
    assert [
        delivery.group_id for delivery in store.due_deliveries("match-1", "map-1", now=500.0)
    ] == [1002]
    assert store.delivery_complete("match-1", "map-1") is False

    dead = store.mark_delivery_failed("match-1", "map-1", 1002, "still unavailable", max_attempts=2)
    assert dead.status == "dead"
    assert dead.attempts == 2
    assert store.delivery_complete("match-1", "map-1") is False
    assert store.delivery_all_sent("match-1", "map-1") is False
    assert store.delivery_summary("match-1", "map-1") == {
        "pending": 0,
        "sent": 1,
        "retry": 0,
        "dead": 1,
        "cancelled": 0,
    }

    assert store.replay_dead(group_id=1002, next_retry_at=600.0) == 1
    replayed = store.get_delivery("match-1", "map-1", 1002)
    assert replayed.status == "retry"
    assert replayed.attempts == 0
    assert store.mark_delivery_sent("match-1", "map-1", 1002).status == "sent"
    assert store.delivery_complete("match-1", "map-1") is True
    assert store.delivery_all_sent("match-1", "map-1") is True

    store.prepare_deliveries("match-2", "map-1", [2001])
    assert store.mark_delivery_sent("match-2", "map-1", 2001).status == "sent"
    assert store.delivery_complete("match-2", "map-1") is True
    assert store.delivery_all_sent("match-2", "map-1") is True


def test_empty_recipient_batch_remains_frozen_and_complete(
    store_factory: Callable[..., Any],
) -> None:
    store = store_factory([])

    assert store.prepare_deliveries("match-empty", "map-1", []) == []
    assert store.prepare_deliveries("match-empty", "map-1", [7777]) == []
    assert store.delivery_complete("match-empty", "map-1") is True
    assert store.delivery_all_sent("match-empty", "map-1") is True


def test_concurrent_subscribe_inserts_a_group_once(
    store_factory: Callable[..., Any],
) -> None:
    store = store_factory([])

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(store.subscribe, [8001] * 8))

    assert sum(results) == 1
    assert store.get_subscriptions() == {8001}


def test_payload_global_due_claim_defer_and_release(
    store_factory: Callable[..., Any],
) -> None:
    store = store_factory([])
    payload = b"\x89PNG\r\n\x1a\ncard bytes"
    store.prepare_deliveries("payload-match", "map-1", [9001, 9002])

    assert store.due_delivery_batches(now=100) == []
    batch = store.set_delivery_payload("payload-match", "map-1", payload)
    assert batch.payload_size == len(payload)
    assert len(batch.payload_sha256) == 64
    assert store.get_delivery_payload("payload-match", "map-1") == payload
    old_payload_path = store.DATA_DIR / batch.payload_path
    payload = b"\x89PNG\r\n\x1a\nreplacement card bytes"
    batch = store.set_delivery_payload("payload-match", "map-1", payload)
    assert store.get_delivery_payload("payload-match", "map-1") == payload
    assert not old_payload_path.exists()
    assert [item.match_id for item in store.due_delivery_batches(now=100)] == ["payload-match"]

    claimed = store.claim_due_deliveries("worker-a", 30, now=100, limit=1)
    assert len(claimed) == 1
    assert claimed[0].claim_owner == "worker-a"
    assert claimed[0].claim_until == 130
    assert len(store.claim_due_deliveries("worker-b", 30, now=100)) == 1

    deferred = store.defer_delivery(
        claimed[0].match_id,
        claimed[0].map_key,
        claimed[0].group_id,
        200,
        "bot offline",
        worker_id="worker-a",
    )
    assert deferred.status == "retry"
    assert deferred.attempts == 0
    assert deferred.claim_owner is None
    assert store.due_deliveries("payload-match", "map-1", now=120) == []

    # worker-b still holds the other row; graceful shutdown releases it immediately.
    assert store.release_claims("worker-b") == 1
    released = store.claim_due_deliveries("worker-c", 30, now=120)
    assert len(released) == 1
    assert (
        store.mark_delivery_sent(
            released[0].match_id,
            released[0].map_key,
            released[0].group_id,
            worker_id="wrong-worker",
        )
        is None
    )
    assert (
        store.mark_delivery_sent(
            released[0].match_id,
            released[0].map_key,
            released[0].group_id,
            worker_id="worker-c",
        ).status
        == "sent"
    )

    overview = store.outbox_overview(now=199)
    assert overview["batches"] == 1
    assert overview["payload_batches"] == 1
    assert overview["payload_bytes"] == len(payload)
    assert overview["retry"] == 1
    assert overview["sent"] == 1
    assert overview["due"] == 0


def test_unsubscribe_cancels_active_deliveries(
    store_factory: Callable[..., Any],
) -> None:
    store = store_factory([])
    assert store.subscribe(9101) is True
    store.prepare_deliveries("cancel-match", "map-1", [9101])
    store.set_delivery_payload("cancel-match", "map-1", b"cancel payload")
    claimed = store.claim_due_deliveries("cancel-worker", 300)
    assert len(claimed) == 1

    assert store.unsubscribe(9101) is True
    delivery = store.get_delivery("cancel-match", "map-1", 9101)
    assert delivery.status == "cancelled"
    assert delivery.cancelled_at is not None
    assert delivery.claim_owner is None
    assert delivery.claim_until is None
    assert store.delivery_complete("cancel-match", "map-1") is True
    assert store.delivery_all_sent("cancel-match", "map-1") is False


def test_payload_atomic_write_failure_keeps_database_pointer_unset(
    store_factory: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = store_factory([])
    store.prepare_deliveries("atomic-match", "map-1", [9151])

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(store.os, "replace", fail_replace)
    with pytest.raises(store.PersistenceError):
        store.set_delivery_payload("atomic-match", "map-1", b"payload")

    batch = store.get_delivery_batch("atomic-match", "map-1")
    assert batch.payload_path is None
    assert list(store.OUTBOX_DIR.iterdir()) == []


def test_retention_removes_only_old_success_or_cancelled_batches(
    store_factory: Callable[..., Any],
) -> None:
    store = store_factory([])
    payload_paths: dict[str, Path] = {}

    store.prepare_deliveries("sent-match", "map-1", [9201])
    sent_batch = store.set_delivery_payload("sent-match", "map-1", b"sent")
    payload_paths["sent"] = store.DATA_DIR / sent_batch.payload_path
    store.mark_delivery_sent("sent-match", "map-1", 9201)

    store.subscribe(9202)
    store.prepare_deliveries("cancelled-match", "map-1", [9202])
    cancelled_batch = store.set_delivery_payload("cancelled-match", "map-1", b"cancelled")
    payload_paths["cancelled"] = store.DATA_DIR / cancelled_batch.payload_path
    store.unsubscribe(9202)

    store.prepare_deliveries("dead-match", "map-1", [9203])
    dead_batch = store.set_delivery_payload("dead-match", "map-1", b"dead")
    payload_paths["dead"] = store.DATA_DIR / dead_batch.payload_path
    store.mark_delivery_dead("dead-match", "map-1", 9203, "permanent failure")

    future = time.time() + 10 * 86400
    assert store.prune_delivery_batches(older_than_days=1, now=future) == 2
    assert store.get_delivery_batch("sent-match", "map-1") is None
    assert store.get_delivery_batch("cancelled-match", "map-1") is None
    assert store.get_delivery_batch("dead-match", "map-1") is not None
    assert not payload_paths["sent"].exists()
    assert not payload_paths["cancelled"].exists()
    assert payload_paths["dead"].exists()


def test_atomic_json_write_failure_is_not_swallowed(
    store_factory: Callable[..., Any],
) -> None:
    store = store_factory([])
    not_a_directory = store.DATA_DIR / "not-a-directory"
    not_a_directory.write_text("file blocks mkdir", encoding="utf-8")
    store.CACHE_DIR = not_a_directory

    with pytest.raises(store.PersistenceError):
        store.cache_set("https://example.test/page", "<html></html>")


def test_page_cache_memory_serves_before_disk(
    store_factory: Callable[..., Any],
) -> None:
    store = store_factory([])
    store._PAGE_MEM.clear()
    url = "https://www.hltv.org/matches-mem"
    store.cache_set(url, "<html>live</html>")
    # Corrupt disk file; memory should still hit.
    path = store._cache_path(url)
    path.write_text("{not-json", encoding="utf-8")
    assert store.cache_get(url, max_age=60) == "<html>live</html>"
    store._PAGE_MEM.clear()
    # Without memory, corrupt disk yields miss / default load failure → None
    assert store.cache_get(url, max_age=60) is None


def test_release_claim_clears_only_owned_row(
    store_factory: Callable[..., Any],
) -> None:
    store = store_factory([1001, 1002])
    store.prepare_deliveries("m1", "map-1", [1001, 1002])
    store.set_delivery_payload("m1", "map-1", b"png")
    claimed = store.claim_due_deliveries("worker-a", lease_seconds=300, limit=10)
    assert {d.group_id for d in claimed} == {1001, 1002}

    assert store.release_claim("m1", "map-1", 1001, worker_id="worker-a") is True
    # Wrong owner cannot clear the other claim
    assert store.release_claim("m1", "map-1", 1002, worker_id="worker-b") is False

    again = store.claim_due_deliveries("worker-b", lease_seconds=300, limit=10)
    assert [d.group_id for d in again] == [1001]
