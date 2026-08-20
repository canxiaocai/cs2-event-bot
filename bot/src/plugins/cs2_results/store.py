"""cs2_results 的持久化。

订阅和逐群投递 outbox 使用 SQLite，其它低争用的页面/logo 缓存仍使用
JSON 和图片文件。SQLite 用**每线程一条**的复用连接跑短事务（见 _transaction）：
连接按 threading.local 存放，因此仍可安全地从 NoneBot 的不同线程/协程调用；
嵌套事务会自动退回「另开一条连接」的老路径。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Literal, Optional

from nonebot.log import logger

# bot 目录 = 本文件往上 4 层(cs2_results/plugins/src/bot)
_BOT_DIR = Path(__file__).resolve().parents[3]
DATA_DIR = _BOT_DIR / "data" / "cs2_results"
LOGO_DIR = DATA_DIR / "logos"
CACHE_DIR = DATA_DIR / "cache"
OUTBOX_DIR = DATA_DIR / "outbox"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGO_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUTBOX_DIR.mkdir(parents=True, exist_ok=True)

_SUBS = DATA_DIR / "subscriptions.json"
_WL = DATA_DIR / "whitelist.json"
_PUSHED = DATA_DIR / "pushed.json"
_EVLOGO = DATA_DIR / "event_logos.json"
_DB = DATA_DIR / "state.sqlite3"

_JSON_LOCK = threading.RLock()
_SCHEMA_VERSION = 5


class PersistenceError(RuntimeError):
    """持久化失败。

    写入函数不会在出错时返回成功；调用方可以据此避免回复用户
    "订阅成功"或过早标记已推送。
    """


class UnsupportedSchemaVersion(PersistenceError):
    """状态库比当前代码新，拒绝用旧代码打开或降级。"""


DeliveryStatus = Literal["pending", "sent", "retry", "dead", "cancelled"]


@dataclass(frozen=True, slots=True)
class Delivery:
    match_id: str
    map_key: str
    group_id: int
    status: DeliveryStatus
    attempts: int
    last_error: Optional[str]
    next_retry_at: Optional[float]
    created_at: float
    updated_at: float
    sent_at: Optional[float]
    cancelled_at: Optional[float]
    claim_owner: Optional[str]
    claim_until: Optional[float]
    # 该群该卡要 @ 的 QQ 列表(个人订阅命中);空=不 @,整群火喇叭照发。
    mentions: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class Target:
    """一条个人订阅:某群某人订阅了某战队/选手。"""
    group_id: int
    qq_user_id: int
    kind: str  # 'team' | 'player'
    target_key: str  # team=casefold 队名;player=HLTV player id(字符串)
    display: str  # 展示原名
    created_at: float


@dataclass(frozen=True, slots=True)
class DeliveryBatch:
    match_id: str
    map_key: str
    created_at: float
    updated_at: float
    payload_path: Optional[str]
    payload_sha256: Optional[str]
    payload_size: Optional[int]
    payload_updated_at: Optional[float]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB, timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


# 每线程复用一条连接。开连接 + 两条 PRAGMA 实测 ~380µs,而在已有连接上跑完一个
# 事务只要 ~4µs——差 ~100 倍,而本模块有 50 处 _transaction、每轮轮询要调几十次。
# sqlite3 连接默认 check_same_thread=True,所以必须按线程存(to_thread 的工作线程
# 各自持有一条,互不干扰)。
_CONN_TLS = threading.local()


def _thread_conn() -> sqlite3.Connection:
    conn = getattr(_CONN_TLS, "conn", None)
    if conn is None:
        conn = _connect()
        _CONN_TLS.conn = conn
    return conn


def _drop_thread_conn() -> None:
    """连接出错后丢弃,下次重开(别让一条坏连接粘住整个线程)。"""
    conn = getattr(_CONN_TLS, "conn", None)
    _CONN_TLS.conn = None
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


@contextmanager
def _transaction(*, write: bool = False) -> Iterator[sqlite3.Connection]:
    # 复用连接后就不能再套娃 BEGIN(同一连接上开第二个事务会直接报错),故嵌套调用
    # 退回「另开一条连接」的老行为——语义与从前完全一致,只是不再享受复用加速。
    if getattr(_CONN_TLS, "busy", False):
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return

    conn = _thread_conn()
    _CONN_TLS.busy = True
    try:
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            _drop_thread_conn()  # 连接已不可用,丢弃重开
        raise
    finally:
        _CONN_TLS.busy = False


def _database_version(conn: sqlite3.Connection) -> int:
    metadata_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'metadata'"
    ).fetchone()
    if not metadata_exists:
        other_tables = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        ).fetchall()
        if other_tables:
            raise PersistenceError("状态库存在未标记版本的表，拒绝自动覆盖")
        return 0
    row = conn.execute("SELECT value FROM metadata WHERE key = 'schema_version'").fetchone()
    if not row:
        raise PersistenceError("状态库缺少 schema_version")
    try:
        version = int(row["value"])
    except (TypeError, ValueError) as e:
        raise PersistenceError("状态库 schema_version 无效") from e
    if version < 1:
        raise PersistenceError(f"不支持的状态库版本: {version}")
    return version


def _create_schema_v2(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        BEGIN IMMEDIATE;
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE subscriptions (
            group_id INTEGER PRIMARY KEY,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE delivery_batches (
            match_id TEXT NOT NULL,
            map_key TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            payload_path TEXT,
            payload_sha256 TEXT,
            payload_size INTEGER CHECK (payload_size IS NULL OR payload_size >= 0),
            payload_updated_at REAL,
            PRIMARY KEY (match_id, map_key)
        );
        CREATE TABLE deliveries (
            match_id TEXT NOT NULL,
            map_key TEXT NOT NULL,
            group_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'sent', 'retry', 'dead', 'cancelled')),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            last_error TEXT,
            next_retry_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            sent_at REAL,
            cancelled_at REAL,
            claim_owner TEXT,
            claim_until REAL,
            PRIMARY KEY (match_id, map_key, group_id),
            FOREIGN KEY (match_id, map_key)
                REFERENCES delivery_batches(match_id, map_key)
                ON DELETE CASCADE
        );
        CREATE INDEX idx_deliveries_due
            ON deliveries(status, next_retry_at, claim_until, match_id, map_key);
        CREATE INDEX idx_delivery_batches_updated
            ON delivery_batches(updated_at);
        INSERT INTO metadata(key, value) VALUES ('schema_version', '2');
        COMMIT;
        """
    )


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """在单一 SQLite 事务中保留 v1 订阅、批次和投递状态。"""
    conn.executescript(
        """
        BEGIN IMMEDIATE;
        ALTER TABLE delivery_batches ADD COLUMN updated_at REAL;
        ALTER TABLE delivery_batches ADD COLUMN payload_path TEXT;
        ALTER TABLE delivery_batches ADD COLUMN payload_sha256 TEXT;
        ALTER TABLE delivery_batches ADD COLUMN payload_size INTEGER;
        ALTER TABLE delivery_batches ADD COLUMN payload_updated_at REAL;
        UPDATE delivery_batches SET updated_at = created_at WHERE updated_at IS NULL;

        DROP INDEX IF EXISTS idx_deliveries_due;
        ALTER TABLE deliveries RENAME TO deliveries_v1;
        CREATE TABLE deliveries (
            match_id TEXT NOT NULL,
            map_key TEXT NOT NULL,
            group_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'sent', 'retry', 'dead', 'cancelled')),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            last_error TEXT,
            next_retry_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            sent_at REAL,
            cancelled_at REAL,
            claim_owner TEXT,
            claim_until REAL,
            PRIMARY KEY (match_id, map_key, group_id),
            FOREIGN KEY (match_id, map_key)
                REFERENCES delivery_batches(match_id, map_key)
                ON DELETE CASCADE
        );
        INSERT INTO deliveries(
            match_id, map_key, group_id, status, attempts, last_error,
            next_retry_at, created_at, updated_at, sent_at
        )
        SELECT match_id, map_key, group_id, status, attempts, last_error,
               next_retry_at, created_at, updated_at, sent_at
        FROM deliveries_v1;
        DROP TABLE deliveries_v1;
        CREATE INDEX idx_deliveries_due
            ON deliveries(status, next_retry_at, claim_until, match_id, map_key);
        CREATE INDEX idx_delivery_batches_updated
            ON delivery_batches(updated_at);
        UPDATE metadata SET value = '2'
            WHERE key = 'schema_version' AND value = '1';
        COMMIT;
        """
    )


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """v3:个人级战队/选手订阅 + 逐群 @ mention。

    - deliveries 增 mentions 列(JSON QQ 列表)
    - subscription_targets:某群某人订阅某战队/选手
    - player_team_cache:选手当前所属队(供 team_hint 零成本命中,免逐场读阵容)
    """
    conn.executescript(
        """
        BEGIN IMMEDIATE;
        ALTER TABLE deliveries ADD COLUMN mentions TEXT;
        CREATE TABLE subscription_targets (
            group_id    INTEGER NOT NULL,
            qq_user_id  INTEGER NOT NULL,
            kind        TEXT NOT NULL CHECK (kind IN ('team', 'player')),
            target_key  TEXT NOT NULL,
            display     TEXT NOT NULL,
            created_at  REAL NOT NULL,
            PRIMARY KEY (group_id, qq_user_id, kind, target_key)
        );
        CREATE INDEX idx_sub_targets_match ON subscription_targets(kind, target_key);
        CREATE TABLE player_team_cache (
            player_id   TEXT PRIMARY KEY,
            nick        TEXT NOT NULL,
            team        TEXT,
            team_key    TEXT,
            updated_at  REAL NOT NULL
        );
        UPDATE metadata SET value = '3' WHERE key = 'schema_version' AND value = '2';
        COMMIT;
        """
    )


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """v4:本地战队名录(team_index)。

    订阅命令不再实时打 HLTV 搜索,改查本地名录:战队来自世界排行榜(低频刷新)+
    追踪比赛顺路收集;选手继续放 player_team_cache(它就是选手名录)。
    """
    conn.executescript(
        """
        BEGIN IMMEDIATE;
        CREATE TABLE team_index (
            team_key    TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            team_id     TEXT,
            rank        INTEGER,
            updated_at  REAL NOT NULL
        );
        UPDATE metadata SET value = '4' WHERE key = 'schema_version' AND value = '3';
        COMMIT;
        """
    )


def _migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    """v5:Valve 世界排名(VRS)本地表。

    渲染 /cs2 日程、赛程 时按队名查它,零 HLTV 请求;数据来自每日一次的总榜全量刷新,
    以及追踪比赛时从比赛页 VRS 面板顺路收下的实时名次(source 区分二者)。
    """
    conn.executescript(
        """
        BEGIN IMMEDIATE;
        CREATE TABLE vrs_ranking (
            team_key    TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            team_id     TEXT,
            rank        INTEGER,
            points      INTEGER,
            region      TEXT,
            source      TEXT NOT NULL DEFAULT 'ranking',
            updated_at  REAL NOT NULL
        );
        UPDATE metadata SET value = '5' WHERE key = 'schema_version' AND value = '4';
        COMMIT;
        """
    )


def _initialize_database() -> None:
    conn = _connect()
    try:
        version = _database_version(conn)
        if version > _SCHEMA_VERSION:
            raise UnsupportedSchemaVersion(
                f"状态库版本为 {version}，当前代码只支持 {_SCHEMA_VERSION}"
            )
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        if version == 0:
            _create_schema_v2(conn)
            _migrate_v2_to_v3(conn)
            _migrate_v3_to_v4(conn)
            _migrate_v4_to_v5(conn)
        elif version == 1:
            _migrate_v1_to_v2(conn)
            _migrate_v2_to_v3(conn)
            _migrate_v3_to_v4(conn)
            _migrate_v4_to_v5(conn)
        elif version == 2:
            _migrate_v2_to_v3(conn)
            _migrate_v3_to_v4(conn)
            _migrate_v4_to_v5(conn)
        elif version == 3:
            _migrate_v3_to_v4(conn)
            _migrate_v4_to_v5(conn)
        elif version == 4:
            _migrate_v4_to_v5(conn)
        elif version != _SCHEMA_VERSION:
            raise PersistenceError(f"不支持的状态库版本: {version}")
        final_version = _database_version(conn)
        if final_version != _SCHEMA_VERSION:
            raise PersistenceError(f"状态库迁移未完成: {final_version} != {_SCHEMA_VERSION}")
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def schema_version() -> int:
    """返回状态库 schema 版本，便于后续显式做 migration。"""
    with _transaction() as conn:
        row = conn.execute("SELECT value FROM metadata WHERE key = 'schema_version'").fetchone()
    return int(row["value"]) if row else 0


def _load(path: Path, default):
    with _JSON_LOCK:
        try:
            if path.exists():
                return json.loads(path.read_text("utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cs2] 读取 {path.name} 失败,用默认值: {e}")
    return default


def _dump(path: Path, obj) -> None:
    """原子写入 JSON；失败时抛出 PersistenceError，不伪装成功。"""
    tmp_path: Optional[Path] = None
    with _JSON_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            tmp_path = None
            try:
                dir_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except Exception as e:  # noqa: BLE001
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            logger.error(f"[cs2] 写入 {path.name} 失败: {e}")
            raise PersistenceError(f"写入 {path} 失败") from e


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """在同目录落盘后原子替换，不暴露半个 payload。"""
    tmp_path: Optional[Path] = None
    with _JSON_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            tmp_path = None
            try:
                dir_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except Exception as e:  # noqa: BLE001
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise PersistenceError(f"写入 payload {path} 失败") from e


# ———————————————————————— 订阅群 ————————————————————————
def _normalize_group_ids(groups: Iterable[int]) -> list[int]:
    result: set[int] = set()
    for group_id in groups:
        try:
            value = int(group_id)
        except (TypeError, ValueError) as e:
            raise ValueError(f"无效群号: {group_id!r}") from e
        if value <= 0:
            raise ValueError(f"无效群号: {group_id!r}")
        result.add(value)
    return sorted(result)


def seed_subscriptions(groups: Iterable[int]) -> int:
    """显式、幂等地把一组订阅写入 DB，返回新增数量。"""
    group_ids = _normalize_group_ids(groups)
    if not group_ids:
        return 0
    now = time.time()
    with _transaction(write=True) as conn:
        before = conn.total_changes
        conn.executemany(
            """
            INSERT OR IGNORE INTO subscriptions(group_id, created_at, updated_at)
            VALUES (?, ?, ?)
            """,
            ((group_id, now, now) for group_id in group_ids),
        )
        return conn.total_changes - before


def seed_subscriptions_once(
    groups: Iterable[int], marker: str = "config_subscription_seed_v1"
) -> int:
    """仅一次把历史配置种子迁入 DB。

    marker 和 inserts 在同一个事务中；空列表也会写 marker。
    启动代码应调用该函数，而不是在每次启动时调用
    ``seed_subscriptions``，否则用户退订的配置群会被复活。
    """
    marker = str(marker).strip()
    if not marker:
        raise ValueError("marker 不能为空")
    group_ids = _normalize_group_ids(groups)
    now = time.time()
    with _transaction(write=True) as conn:
        if conn.execute("SELECT 1 FROM metadata WHERE key = ?", (marker,)).fetchone():
            return 0
        before = conn.total_changes
        conn.executemany(
            """
            INSERT OR IGNORE INTO subscriptions(group_id, created_at, updated_at)
            VALUES (?, ?, ?)
            """,
            ((group_id, now, now) for group_id in group_ids),
        )
        added = conn.total_changes - before
        conn.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (marker, str(now)),
        )
        return added


def migrate_legacy_subscriptions(path: Path = _SUBS) -> int:
    """一次性迁移旧 ``subscriptions.json``，返回新增行数。

    migration marker 与订阅写入在同一事务中提交。文件不存在也会
    记录已检查；文件损坏则抛错且不写 marker，修复后可重试。
    """
    marker = "subscriptions_json_migrated_v1"
    with _transaction() as conn:
        if conn.execute("SELECT 1 FROM metadata WHERE key = ?", (marker,)).fetchone():
            return 0

    if path.exists():
        try:
            raw = json.loads(path.read_text("utf-8"))
        except Exception as e:  # noqa: BLE001
            raise PersistenceError(f"读取旧订阅文件 {path} 失败") from e
        if not isinstance(raw, list):
            raise PersistenceError(f"旧订阅文件 {path} 必须是 JSON 数组")
        try:
            group_ids = _normalize_group_ids(raw)
        except ValueError as e:
            raise PersistenceError(f"旧订阅文件 {path} 包含无效群号") from e
    else:
        group_ids = []

    now = time.time()
    with _transaction(write=True) as conn:
        if conn.execute("SELECT 1 FROM metadata WHERE key = ?", (marker,)).fetchone():
            return 0
        before = conn.total_changes
        conn.executemany(
            """
            INSERT OR IGNORE INTO subscriptions(group_id, created_at, updated_at)
            VALUES (?, ?, ?)
            """,
            ((group_id, now, now) for group_id in group_ids),
        )
        added = conn.total_changes - before
        conn.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (marker, str(now)),
        )
    if path.exists():
        logger.info(f"[cs2] 已从 subscriptions.json 迁移 {added} 个订阅群")
    return added


def get_subscriptions() -> set[int]:
    with _transaction() as conn:
        rows = conn.execute("SELECT group_id FROM subscriptions").fetchall()
    return {int(row["group_id"]) for row in rows}


def subscription_count() -> int:
    with _transaction() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM subscriptions").fetchone()
    return int(row["n"])


def subscribe(group_id: int) -> bool:
    group_id = _normalize_group_ids([group_id])[0]
    now = time.time()
    with _transaction(write=True) as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO subscriptions(group_id, created_at, updated_at)
            VALUES (?, ?, ?)
            """,
            (group_id, now, now),
        )
        return cursor.rowcount == 1


def unsubscribe(group_id: int) -> bool:
    group_id = _normalize_group_ids([group_id])[0]
    now = time.time()
    with _transaction(write=True) as conn:
        cursor = conn.execute("DELETE FROM subscriptions WHERE group_id = ?", (group_id,))
        affected = conn.execute(
            """
            SELECT DISTINCT match_id, map_key FROM deliveries
            WHERE group_id = ? AND status IN ('pending', 'retry')
            """,
            (group_id,),
        ).fetchall()
        conn.execute(
            """
            UPDATE deliveries
            SET status = 'cancelled', last_error = 'subscription removed',
                next_retry_at = NULL, updated_at = ?, cancelled_at = ?,
                claim_owner = NULL, claim_until = NULL
            WHERE group_id = ? AND status IN ('pending', 'retry')
            """,
            (now, now, group_id),
        )
        conn.executemany(
            """
            UPDATE delivery_batches SET updated_at = ?
            WHERE match_id = ? AND map_key = ?
            """,
            ((now, row["match_id"], row["map_key"]) for row in affected),
        )
        # 群级火喇叭关掉后,该群的个人级战队/选手订阅也无处依附(门槛:必须先开火喇叭),
        # 一并清掉,避免"我明明订阅了却不推"的困惑与孤儿数据。
        conn.execute("DELETE FROM subscription_targets WHERE group_id = ?", (group_id,))
        return cursor.rowcount == 1


# —————————— 个人级战队/选手订阅 ——————————
def _target_from_row(row: sqlite3.Row) -> Target:
    return Target(
        group_id=int(row["group_id"]),
        qq_user_id=int(row["qq_user_id"]),
        kind=str(row["kind"]),
        target_key=str(row["target_key"]),
        display=str(row["display"]),
        created_at=float(row["created_at"]),
    )


def add_target(
    group_id: int,
    qq_user_id: int,
    kind: str,
    target_key: str,
    display: str,
    *,
    max_per_user: int = 20,
) -> str:
    """新增一条个人订阅。返回 'added' / 'exists' / 'full'。"""
    if kind not in ("team", "player"):
        raise ValueError(f"非法订阅类型: {kind!r}")
    group_id = _normalize_group_ids([group_id])[0]
    qq_user_id = int(qq_user_id)
    target_key = str(target_key).strip()
    display = str(display).strip()
    if not target_key or not display:
        raise ValueError("target_key / display 不能为空")
    now = time.time()
    with _transaction(write=True) as conn:
        exists = conn.execute(
            """SELECT 1 FROM subscription_targets
               WHERE group_id=? AND qq_user_id=? AND kind=? AND target_key=?""",
            (group_id, qq_user_id, kind, target_key),
        ).fetchone()
        if exists:
            return "exists"
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM subscription_targets WHERE group_id=? AND qq_user_id=?",
            (group_id, qq_user_id),
        ).fetchone()["n"]
        if int(n) >= max_per_user:
            return "full"
        conn.execute(
            """INSERT INTO subscription_targets
               (group_id, qq_user_id, kind, target_key, display, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (group_id, qq_user_id, kind, target_key, display, now),
        )
        return "added"


def remove_target(group_id: int, qq_user_id: int, kind: str, target_key: str) -> bool:
    group_id = _normalize_group_ids([group_id])[0]
    with _transaction(write=True) as conn:
        cur = conn.execute(
            """DELETE FROM subscription_targets
               WHERE group_id=? AND qq_user_id=? AND kind=? AND target_key=?""",
            (group_id, int(qq_user_id), kind, str(target_key).strip()),
        )
        return cur.rowcount > 0


def list_targets(group_id: int, qq_user_id: Optional[int] = None) -> list[Target]:
    group_id = _normalize_group_ids([group_id])[0]
    with _transaction() as conn:
        if qq_user_id is None:
            rows = conn.execute(
                "SELECT * FROM subscription_targets WHERE group_id=? ORDER BY qq_user_id, kind, display",
                (group_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM subscription_targets
                   WHERE group_id=? AND qq_user_id=? ORDER BY kind, display""",
                (group_id, int(qq_user_id)),
            ).fetchall()
    return [_target_from_row(r) for r in rows]


def all_targets() -> list[Target]:
    with _transaction() as conn:
        rows = conn.execute("SELECT * FROM subscription_targets").fetchall()
    return [_target_from_row(r) for r in rows]


def distinct_target_keys() -> tuple[set[str], set[str]]:
    """(全部被订阅的 team_key, 全部被订阅的 player_id) —— 供扫描期快速判断是否需要关注。"""
    with _transaction() as conn:
        rows = conn.execute("SELECT DISTINCT kind, target_key FROM subscription_targets").fetchall()
    teams = {r["target_key"] for r in rows if r["kind"] == "team"}
    players = {r["target_key"] for r in rows if r["kind"] == "player"}
    return teams, players


def recipients_for(
    candidate_groups: Iterable[int],
    team_keys: Iterable[str],
    player_ids: Iterable[str],
) -> dict[int, set[int]]:
    """在给定候选群(火喇叭群)里,找出订阅了本场任一战队/选手的 (群→{QQ})。"""
    groups = _normalize_group_ids(candidate_groups)
    tkeys = [k for k in {str(k) for k in team_keys} if k]
    pids = [k for k in {str(k) for k in player_ids} if k]
    if not groups or (not tkeys and not pids):
        return {}
    gph = ",".join("?" * len(groups))
    conds = []
    params: list = list(groups)
    if tkeys:
        conds.append(f"(kind='team' AND target_key IN ({','.join('?' * len(tkeys))}))")
        params += tkeys
    if pids:
        conds.append(f"(kind='player' AND target_key IN ({','.join('?' * len(pids))}))")
        params += pids
    sql = (
        f"SELECT group_id, qq_user_id FROM subscription_targets "
        f"WHERE group_id IN ({gph}) AND ({' OR '.join(conds)})"
    )
    out: dict[int, set[int]] = {}
    with _transaction() as conn:
        for row in conn.execute(sql, params).fetchall():
            out.setdefault(int(row["group_id"]), set()).add(int(row["qq_user_id"]))
    return out


def prune_user_targets(group_id: int, qq_user_id: int) -> int:
    """成员退群:清掉其在该群的全部个人订阅。"""
    group_id = _normalize_group_ids([group_id])[0]
    with _transaction(write=True) as conn:
        cur = conn.execute(
            "DELETE FROM subscription_targets WHERE group_id=? AND qq_user_id=?",
            (group_id, int(qq_user_id)),
        )
        return cur.rowcount


# —————————— 选手当前所属队缓存(供 team_hint 零成本命中)——————————
def get_player_team(player_id: str) -> Optional[dict]:
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM player_team_cache WHERE player_id=?", (str(player_id),)
        ).fetchone()
    if not row:
        return None
    return {
        "player_id": str(row["player_id"]),
        "nick": row["nick"],
        "team": row["team"],
        "team_key": row["team_key"],
        "updated_at": float(row["updated_at"]),
    }


def get_player_team_keys(player_ids: Iterable[str]) -> set[str]:
    """一次查出这批选手当前所属队的 team_key(空的略过)。

    对应 ``get_player_team`` 的批量版:每轮 scan_live 都要把全部被订阅选手过一遍,
    逐个查等于每人一个事务,人数一多光连接开销就压满一段循环。
    """
    ids = [str(i) for i in {str(i) for i in player_ids} if i]
    if not ids:
        return set()
    with _transaction() as conn:
        rows = conn.execute(
            f"""SELECT team_key FROM player_team_cache
                WHERE player_id IN ({','.join('?' * len(ids))})""",
            ids,
        ).fetchall()
    return {r["team_key"] for r in rows if r["team_key"]}


def set_player_team(player_id: str, nick: str, team: Optional[str], team_key: Optional[str]) -> None:
    set_player_teams([(player_id, nick, team, team_key)])


def set_player_teams(rows: Iterable[tuple[str, str, Optional[str], Optional[str]]]) -> None:
    """批量写入 (player_id, nick, team, team_key)。

    一场比赛的首发是 10 人,逐个写就是 10 个 ``BEGIN IMMEDIATE`` + 提交;
    合成一个事务里的 executemany 只要一次。
    """
    now = time.time()
    payload = [(str(pid), str(nick), team, team_key, now) for pid, nick, team, team_key in rows]
    if not payload:
        return
    with _transaction(write=True) as conn:
        conn.executemany(
            """INSERT INTO player_team_cache(player_id, nick, team, team_key, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(player_id) DO UPDATE SET
                 nick=excluded.nick, team=excluded.team,
                 team_key=excluded.team_key, updated_at=excluded.updated_at""",
            payload,
        )


def players_needing_team_refresh(subscribed_ids: Iterable[str], older_than_s: float) -> list[str]:
    """订阅中的选手里,team 缓存缺失或过期的 player_id 列表(需重新解析所属队)。"""
    ids = [str(i) for i in {str(i) for i in subscribed_ids} if i]
    if not ids:
        return []
    cutoff = time.time() - older_than_s
    with _transaction() as conn:
        rows = conn.execute(
            f"""SELECT player_id, updated_at, team_key FROM player_team_cache
                WHERE player_id IN ({','.join('?' * len(ids))})""",
            ids,
        ).fetchall()
    fresh = {str(r["player_id"]) for r in rows if float(r["updated_at"]) >= cutoff and r["team_key"]}
    return [i for i in ids if i not in fresh]


# —————————— 本地名录(战队 team_index + 选手 player_team_cache) ——————————
def get_meta(key: str) -> Optional[str]:
    with _transaction() as conn:
        row = conn.execute("SELECT value FROM metadata WHERE key = ?", (str(key),)).fetchone()
    return str(row["value"]) if row else None


def set_meta(key: str, value: str) -> None:
    with _transaction(write=True) as conn:
        conn.execute(
            """INSERT INTO metadata(key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (str(key), str(value)),
        )


def upsert_ranking(teams: Iterable) -> tuple[int, int]:
    """把排行榜(RankedTeam:rank/name/team_id/players)整体写入名录,一个事务。

    返回 (战队数, 选手数)。选手写进 player_team_cache(它就是选手名录),
    所属队 = 榜单上的队 → 顺带解决转会后 team_hint 过期的问题。
    """
    now = time.time()
    n_teams = n_players = 0
    with _transaction(write=True) as conn:
        for t in teams:
            name = str(getattr(t, "name", "") or "").strip()
            if not name:
                continue
            conn.execute(
                """INSERT INTO team_index(team_key, name, team_id, rank, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(team_key) DO UPDATE SET
                     name=excluded.name, team_id=excluded.team_id,
                     rank=excluded.rank, updated_at=excluded.updated_at""",
                (name.casefold(), name, getattr(t, "team_id", None),
                 getattr(t, "rank", None), now),
            )
            n_teams += 1
            for pid, nick in getattr(t, "players", []) or []:
                pid = str(pid).strip()
                nick = str(nick).strip()
                if not pid or not nick:
                    continue
                conn.execute(
                    """INSERT INTO player_team_cache(player_id, nick, team, team_key, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(player_id) DO UPDATE SET
                         nick=excluded.nick, team=excluded.team,
                         team_key=excluded.team_key, updated_at=excluded.updated_at""",
                    (pid, nick, name, name.casefold(), now),
                )
                n_players += 1
    return n_teams, n_players


def note_teams_seen(names_list: Iterable[str]) -> None:
    """顺路收集:把比赛页/列表页上见到的队名并进名录(不覆盖排行榜写入的 rank/team_id)。"""
    now = time.time()
    rows = [(n.strip().casefold(), n.strip(), now) for n in names_list if n and n.strip()]
    if not rows:
        return
    with _transaction(write=True) as conn:
        conn.executemany(
            """INSERT INTO team_index(team_key, name, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(team_key) DO UPDATE SET
                 name=excluded.name, updated_at=excluded.updated_at""",
            rows,
        )


def all_index_teams() -> list[dict]:
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT team_key, name, team_id, rank FROM team_index ORDER BY rank IS NULL, rank"
        ).fetchall()
    return [
        {"team_key": r["team_key"], "name": r["name"], "team_id": r["team_id"], "rank": r["rank"]}
        for r in rows
    ]


def all_index_players() -> list[dict]:
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT player_id, nick, team, team_key FROM player_team_cache"
        ).fetchall()
    return [
        {"player_id": r["player_id"], "nick": r["nick"], "team": r["team"], "team_key": r["team_key"]}
        for r in rows
    ]


def roster_overview() -> dict:
    with _transaction() as conn:
        t = conn.execute("SELECT COUNT(*) AS n FROM team_index").fetchone()["n"]
        p = conn.execute("SELECT COUNT(*) AS n FROM player_team_cache").fetchone()["n"]
    ts = get_meta("roster_refreshed_at")
    return {"teams": int(t), "players": int(p), "refreshed_at": float(ts) if ts else None}


# —————————— Valve 世界排名(VRS) ——————————
def _pick_vrs_entry(cands: list, rosters: dict[str, set[str]]) -> object:
    """同名多条(VRS 排的是阵容不是俱乐部)时挑出在役的那条。

    判据:与本地名录里该队**当前阵容**的 player_id 重合度最高;名录里没这支队(或并列)
    就退回名次更靠前的一条——老阵容的分会随时间衰减,通常排在新阵容后面。
    """
    if len(cands) == 1:
        return cands[0]
    roster = rosters.get(str(getattr(cands[0], "name", "")).strip().casefold(), set())

    def score(c) -> tuple[int, int]:
        overlap = len(roster & set(getattr(c, "players", []) or []))
        rank = getattr(c, "rank", None)
        return (overlap, -(rank if rank else 10**6))

    return max(cands, key=score)


def upsert_vrs_ranking(teams: Iterable, keep_match_window_s: float = 3600.0) -> int:
    """把 VRS 总榜整体写入 vrs_ranking(一个事务,替换语义)。

    同名条目先按在役阵容收敛成一条;写完删掉本次快照没覆盖到的旧行,保证表里不留已掉榜
    / 已改名的陈旧名次。返回写入的队伍数。

    一个例外:总榜是 HLTV **当天零点前后**生成的静态快照,而从比赛页顺路收下的名次是
    赛果一出就写的。刚收到不久(keep_match_window_s 内)的实时名次因此比快照更新,保留
    不覆盖——否则一场刚打完的比赛会被一份更早生成的快照按回旧名次。
    """
    now = time.time()
    grouped: dict[str, list] = {}
    for t in teams:
        name = str(getattr(t, "name", "") or "").strip()
        if name:
            grouped.setdefault(name.casefold(), []).append(t)
    if not grouped:
        return 0
    with _transaction(write=True) as conn:
        rosters: dict[str, set[str]] = {}
        for row in conn.execute(
            "SELECT team_key, player_id FROM player_team_cache WHERE team_key IS NOT NULL"
        ):
            rosters.setdefault(row["team_key"], set()).add(str(row["player_id"]))
        keep = {
            row["team_key"]
            for row in conn.execute(
                "SELECT team_key FROM vrs_ranking WHERE source = 'match' AND updated_at > ?",
                (now - max(0.0, keep_match_window_s),),
            )
        }
        for key, cands in grouped.items():
            if key in keep:
                # 只把时间戳推到本轮,让下面按 updated_at 的清理不会误删这一行
                conn.execute(
                    "UPDATE vrs_ranking SET updated_at = ? WHERE team_key = ?", (now, key)
                )
                continue
            t = _pick_vrs_entry(cands, rosters)
            conn.execute(
                """INSERT INTO vrs_ranking(
                       team_key, name, team_id, rank, points, region, source, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'ranking', ?)
                   ON CONFLICT(team_key) DO UPDATE SET
                     name=excluded.name, team_id=excluded.team_id, rank=excluded.rank,
                     points=excluded.points, region=excluded.region,
                     source=excluded.source, updated_at=excluded.updated_at""",
                (
                    key,
                    str(getattr(t, "name", "")).strip(),
                    getattr(t, "team_id", None),
                    getattr(t, "rank", None),
                    getattr(t, "points", None),
                    getattr(t, "region", "") or "",
                    now,
                ),
            )
        conn.execute("DELETE FROM vrs_ranking WHERE updated_at < ?", (now,))
    return len(grouped)


def note_vrs_from_match(pairs: Iterable[tuple[str, Optional[int], Optional[int]]]) -> int:
    """顺路收:把比赛页 VRS 面板上的 (队名, 名次, 积分) 写进表里,零额外请求。

    比赛页给的是该场**这套阵容**的当前名次,比每日总榜更即时(赛果一出就变),所以
    直接覆盖总榜那一行;没在总榜里的队也会新建行(下次全量刷新时若仍未上榜会被清掉)。
    """
    now = time.time()
    rows = [
        (name.strip().casefold(), name.strip(), rank, points, now)
        for name, rank, points in pairs
        if name and name.strip() and rank
    ]
    if not rows:
        return 0
    with _transaction(write=True) as conn:
        conn.executemany(
            """INSERT INTO vrs_ranking(team_key, name, rank, points, source, updated_at)
               VALUES (?, ?, ?, ?, 'match', ?)
               ON CONFLICT(team_key) DO UPDATE SET
                 name=excluded.name, rank=excluded.rank,
                 points=COALESCE(excluded.points, vrs_ranking.points),
                 source=excluded.source, updated_at=excluded.updated_at""",
            rows,
        )
    return len(rows)


def vrs_ranks() -> dict[str, int]:
    """{casefold 队名: VRS 名次} —— 渲染卡片时一次查全表(几百行),之后纯内存命中。"""
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT team_key, rank FROM vrs_ranking WHERE rank IS NOT NULL"
        ).fetchall()
    return {r["team_key"]: int(r["rank"]) for r in rows}


def vrs_overview() -> dict:
    with _transaction() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM vrs_ranking").fetchone()["n"]
        m = conn.execute(
            "SELECT COUNT(*) AS n FROM vrs_ranking WHERE source = 'match'"
        ).fetchone()["n"]
    ts = get_meta("vrs_refreshed_at")
    return {
        "teams": int(n),
        "from_match": int(m),
        "refreshed_at": float(ts) if ts else None,
        "snapshot": get_meta("vrs_snapshot_date") or "",
    }


# —————————— 逐群投递 outbox ——————————
def _parse_mentions(raw) -> tuple[int, ...]:
    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    out: list[int] = []
    seen: set[int] = set()
    for v in data if isinstance(data, list) else ():
        try:
            q = int(v)
        except (TypeError, ValueError):
            continue
        if q > 0 and q not in seen:
            seen.add(q)
            out.append(q)
    return tuple(out)


def _delivery_from_row(row: sqlite3.Row) -> Delivery:
    keys = row.keys()
    return Delivery(
        match_id=str(row["match_id"]),
        map_key=str(row["map_key"]),
        group_id=int(row["group_id"]),
        status=row["status"],
        attempts=int(row["attempts"]),
        last_error=row["last_error"],
        next_retry_at=row["next_retry_at"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        sent_at=row["sent_at"],
        cancelled_at=row["cancelled_at"],
        claim_owner=row["claim_owner"],
        claim_until=row["claim_until"],
        mentions=_parse_mentions(row["mentions"] if "mentions" in keys else None),
    )


def _batch_from_row(row: sqlite3.Row) -> DeliveryBatch:
    return DeliveryBatch(
        match_id=str(row["match_id"]),
        map_key=str(row["map_key"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        payload_path=row["payload_path"],
        payload_sha256=row["payload_sha256"],
        payload_size=row["payload_size"],
        payload_updated_at=row["payload_updated_at"],
    )


def _validate_delivery_key(match_id: str, map_key: str) -> tuple[str, str]:
    match_id = str(match_id).strip()
    map_key = str(map_key).strip()
    if not match_id or not map_key:
        raise ValueError("match_id 和 map_key 不能为空")
    return match_id, map_key


def _safe_outbox_path(relative_path: str) -> Path:
    path = (DATA_DIR / relative_path).resolve()
    root = OUTBOX_DIR.resolve()
    if not path.is_relative_to(root):
        raise PersistenceError(f"非法 payload 路径: {relative_path!r}")
    return path


def _mentions_blob(qq_ids: Optional[Iterable[int]]) -> Optional[str]:
    """把该群要 @ 的 QQ 列表序列化成 deliveries.mentions 存储值;空 → NULL。"""
    if not qq_ids:
        return None
    out: list[int] = []
    seen: set[int] = set()
    for v in qq_ids:
        try:
            q = int(v)
        except (TypeError, ValueError):
            continue
        if q > 0 and q not in seen:
            seen.add(q)
            out.append(q)
    return json.dumps(out) if out else None


def prepare_deliveries(
    match_id: str,
    map_key: str,
    group_ids: Iterable[int],
    mentions: Optional[dict[int, Iterable[int]]] = None,
) -> list[Delivery]:
    """首次调用时冻结该地图的收件群，后续调用只返回已冻结集合。

    ``delivery_batches`` 会记录空收件人批次，因此之后的新订阅群也
    不会被补进旧地图。批次与所有投递行在同一事务中创建。

    ``mentions``:{group_id: [QQ...]},每群该卡要 @ 的人(个人订阅命中)。
    与收件集一起在首次调用时冻结。
    """
    match_id, map_key = _validate_delivery_key(match_id, map_key)
    recipients = _normalize_group_ids(group_ids)
    mention_map = {int(g): v for g, v in (mentions or {}).items()}
    now = time.time()
    with _transaction(write=True) as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO delivery_batches(match_id, map_key, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (match_id, map_key, now, now),
        )
        if cursor.rowcount == 1:
            conn.executemany(
                """
                INSERT INTO deliveries(
                    match_id, map_key, group_id, status, attempts,
                    created_at, updated_at, mentions
                ) VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (
                    (match_id, map_key, group_id, now, now,
                     _mentions_blob(mention_map.get(group_id)))
                    for group_id in recipients
                ),
            )
        rows = conn.execute(
            """
            SELECT * FROM deliveries
            WHERE match_id = ? AND map_key = ?
            ORDER BY group_id
            """,
            (match_id, map_key),
        ).fetchall()
    return [_delivery_from_row(row) for row in rows]


def get_delivery_batch(match_id: str, map_key: str) -> Optional[DeliveryBatch]:
    match_id, map_key = _validate_delivery_key(match_id, map_key)
    with _transaction() as conn:
        row = conn.execute(
            """
            SELECT * FROM delivery_batches WHERE match_id = ? AND map_key = ?
            """,
            (match_id, map_key),
        ).fetchone()
    return _batch_from_row(row) if row else None


def set_delivery_payload(match_id: str, map_key: str, payload: bytes) -> DeliveryBatch:
    """线程安全地为已 prepare 的批次持久化 payload。"""
    with _JSON_LOCK:
        return _set_delivery_payload_locked(match_id, map_key, payload)


def _set_delivery_payload_locked(match_id: str, map_key: str, payload: bytes) -> DeliveryBatch:
    """为已 prepare 的批次持久化可重试 payload，返回新批次元数据。

    文件名同时包含 batch key 和内容哈希。先原子写入新内容地址，
    再在 DB 中切换指针；事务失败不会破坏旧 payload。
    """
    match_id, map_key = _validate_delivery_key(match_id, map_key)
    if not isinstance(payload, bytes) or not payload:
        raise ValueError("payload 必须是非空 bytes")
    with _transaction() as conn:
        old = conn.execute(
            """
            SELECT payload_path FROM delivery_batches
            WHERE match_id = ? AND map_key = ?
            """,
            (match_id, map_key),
        ).fetchone()
    if not old:
        raise KeyError(f"投递批次不存在: {match_id}/{map_key}")

    digest = hashlib.sha256(payload).hexdigest()
    batch_hash = hashlib.sha256(f"{match_id}\0{map_key}".encode()).hexdigest()[:24]
    relative = f"outbox/{batch_hash}-{digest}.bin"
    payload_path = _safe_outbox_path(relative)
    existed = payload_path.exists()
    _atomic_write_bytes(payload_path, payload)

    now = time.time()
    try:
        with _transaction(write=True) as conn:
            cursor = conn.execute(
                """
                UPDATE delivery_batches
                SET payload_path = ?, payload_sha256 = ?, payload_size = ?,
                    payload_updated_at = ?, updated_at = ?
                WHERE match_id = ? AND map_key = ?
                """,
                (relative, digest, len(payload), now, now, match_id, map_key),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"投递批次不存在: {match_id}/{map_key}")
            row = conn.execute(
                """
                SELECT * FROM delivery_batches WHERE match_id = ? AND map_key = ?
                """,
                (match_id, map_key),
            ).fetchone()
    except Exception:
        if not existed:
            try:
                payload_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise

    old_relative = old["payload_path"]
    if old_relative and old_relative != relative:
        try:
            _safe_outbox_path(str(old_relative)).unlink(missing_ok=True)
        except (OSError, PersistenceError) as e:
            logger.warning(f"[cs2] 清理旧 payload 失败: {e}")
    return _batch_from_row(row)


def get_delivery_payload(match_id: str, map_key: str) -> Optional[bytes]:
    """读取并校验批次 payload；未设置返回 None，丢失/损坏则抛错。"""
    with _JSON_LOCK:
        return _get_delivery_payload_locked(match_id, map_key)


def _get_delivery_payload_locked(match_id: str, map_key: str) -> Optional[bytes]:
    batch = get_delivery_batch(match_id, map_key)
    if not batch or not batch.payload_path:
        return None
    path = _safe_outbox_path(batch.payload_path)
    try:
        payload = path.read_bytes()
    except OSError as e:
        raise PersistenceError(f"payload 不可读: {match_id}/{map_key}") from e
    digest = hashlib.sha256(payload).hexdigest()
    if len(payload) != batch.payload_size or digest != batch.payload_sha256:
        raise PersistenceError(f"payload 校验失败: {match_id}/{map_key}")
    return payload


def list_deliveries(match_id: str, map_key: str) -> list[Delivery]:
    match_id, map_key = _validate_delivery_key(match_id, map_key)
    with _transaction() as conn:
        rows = conn.execute(
            """
            SELECT * FROM deliveries
            WHERE match_id = ? AND map_key = ?
            ORDER BY group_id
            """,
            (match_id, map_key),
        ).fetchall()
    return [_delivery_from_row(row) for row in rows]


def due_deliveries(
    match_id: str,
    map_key: str,
    now: Optional[float] = None,
    *,
    limit: Optional[int] = None,
) -> list[Delivery]:
    """返回 pending 及到期 retry 行，不会改变状态或尝试次数。"""
    match_id, map_key = _validate_delivery_key(match_id, map_key)
    now = time.time() if now is None else float(now)
    sql = """
        SELECT * FROM deliveries
        WHERE match_id = ? AND map_key = ?
          AND (
            status = 'pending'
            OR (status = 'retry' AND (next_retry_at IS NULL OR next_retry_at <= ?))
          )
          AND (claim_until IS NULL OR claim_until <= ?)
        ORDER BY group_id
    """
    params: list[object] = [match_id, map_key, now, now]
    if limit is not None:
        if limit <= 0:
            return []
        sql += " LIMIT ?"
        params.append(int(limit))
    with _transaction() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_delivery_from_row(row) for row in rows]


def due_delivery_batches(now: Optional[float] = None, limit: int = 100) -> list[DeliveryBatch]:
    """返回有 payload 且至少一条未被 lease 的到期投递批次。"""
    if limit <= 0:
        return []
    now = time.time() if now is None else float(now)
    with _transaction() as conn:
        rows = conn.execute(
            """
            SELECT b.* FROM delivery_batches AS b
            WHERE b.payload_path IS NOT NULL
              AND EXISTS (
                SELECT 1 FROM deliveries AS d
                WHERE d.match_id = b.match_id AND d.map_key = b.map_key
                  AND (
                    d.status = 'pending'
                    OR (d.status = 'retry'
                        AND (d.next_retry_at IS NULL OR d.next_retry_at <= ?))
                  )
                  AND (d.claim_until IS NULL OR d.claim_until <= ?)
              )
            ORDER BY b.updated_at, b.match_id, b.map_key
            LIMIT ?
            """,
            (now, now, int(limit)),
        ).fetchall()
    return [_batch_from_row(row) for row in rows]


def claim_due_deliveries(
    worker_id: str,
    lease_seconds: float,
    now: Optional[float] = None,
    limit: int = 100,
) -> list[Delivery]:
    """原子领取全局到期投递，供独立 consumer 避免重复发送。

    只领取已持久化 payload 的批次。lease 过期后会自动重新可领取，
    领取本身不增加 attempts。
    """
    worker_id = str(worker_id).strip()
    if not worker_id:
        raise ValueError("worker_id 不能为空")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds 必须大于 0")
    if limit <= 0:
        return []
    now = time.time() if now is None else float(now)
    claim_until = now + float(lease_seconds)
    with _transaction(write=True) as conn:
        selected = conn.execute(
            """
            SELECT d.match_id, d.map_key, d.group_id
            FROM deliveries AS d
            JOIN delivery_batches AS b
              ON b.match_id = d.match_id AND b.map_key = d.map_key
            WHERE b.payload_path IS NOT NULL
              AND (
                d.status = 'pending'
                OR (d.status = 'retry'
                    AND (d.next_retry_at IS NULL OR d.next_retry_at <= ?))
              )
              AND (d.claim_until IS NULL OR d.claim_until <= ?)
            ORDER BY COALESCE(d.next_retry_at, d.created_at),
                     d.match_id, d.map_key, d.group_id
            LIMIT ?
            """,
            (now, now, int(limit)),
        ).fetchall()
        keys = [(row["match_id"], row["map_key"], int(row["group_id"])) for row in selected]
        conn.executemany(
            """
            UPDATE deliveries SET claim_owner = ?, claim_until = ?, updated_at = ?
            WHERE match_id = ? AND map_key = ? AND group_id = ?
              AND status IN ('pending', 'retry')
            """,
            (
                (worker_id, claim_until, now, match_id, map_key, group_id)
                for match_id, map_key, group_id in keys
            ),
        )
        rows = [
            conn.execute(
                """
                SELECT * FROM deliveries
                WHERE match_id = ? AND map_key = ? AND group_id = ?
                """,
                key,
            ).fetchone()
            for key in keys
        ]
    return [_delivery_from_row(row) for row in rows if row is not None]


def get_delivery(match_id: str, map_key: str, group_id: int) -> Optional[Delivery]:
    match_id, map_key = _validate_delivery_key(match_id, map_key)
    group_id = _normalize_group_ids([group_id])[0]
    with _transaction() as conn:
        row = conn.execute(
            """
            SELECT * FROM deliveries
            WHERE match_id = ? AND map_key = ? AND group_id = ?
            """,
            (match_id, map_key, group_id),
        ).fetchone()
    return _delivery_from_row(row) if row else None


def mark_delivery_sent(
    match_id: str,
    map_key: str,
    group_id: int,
    *,
    worker_id: Optional[str] = None,
) -> Optional[Delivery]:
    """只有真实发送成功后才调用；幂等地保护 sent/dead 终态。"""
    match_id, map_key = _validate_delivery_key(match_id, map_key)
    group_id = _normalize_group_ids([group_id])[0]
    if worker_id is not None:
        worker_id = str(worker_id).strip()
        if not worker_id:
            raise ValueError("worker_id 不能为空")
    now = time.time()
    with _transaction(write=True) as conn:
        claim_clause = " AND claim_owner = ?" if worker_id is not None else ""
        params: list[object] = [now, now, match_id, map_key, group_id]
        if worker_id is not None:
            params.append(worker_id)
        cursor = conn.execute(
            f"""
            UPDATE deliveries
            SET status = 'sent', attempts = attempts + 1,
                last_error = NULL, next_retry_at = NULL,
                updated_at = ?, sent_at = ?, cancelled_at = NULL,
                claim_owner = NULL, claim_until = NULL
            WHERE match_id = ? AND map_key = ? AND group_id = ?
              AND status IN ('pending', 'retry')
              {claim_clause}
            """,
            params,
        )
        if cursor.rowcount != 1:
            return None
        conn.execute(
            """
            UPDATE delivery_batches SET updated_at = ?
            WHERE match_id = ? AND map_key = ?
            """,
            (now, match_id, map_key),
        )
        row = conn.execute(
            """
            SELECT * FROM deliveries
            WHERE match_id = ? AND map_key = ? AND group_id = ?
            """,
            (match_id, map_key, group_id),
        ).fetchone()
    return _delivery_from_row(row)


def mark_delivery_failed(
    match_id: str,
    map_key: str,
    group_id: int,
    error: object,
    *,
    next_retry_at: Optional[float] = None,
    dead: bool = False,
    max_attempts: Optional[int] = None,
    worker_id: Optional[str] = None,
) -> Optional[Delivery]:
    """记录一次失败，并进入 retry 或 dead。

    ``next_retry_at`` 是 epoch 秒；为 None 时 retry 会立即到期。
    ``max_attempts`` 包含本次失败，达到上限时自动进入 dead。
    """
    match_id, map_key = _validate_delivery_key(match_id, map_key)
    group_id = _normalize_group_ids([group_id])[0]
    if max_attempts is not None and max_attempts <= 0:
        raise ValueError("max_attempts 必须大于 0")
    if worker_id is not None:
        worker_id = str(worker_id).strip()
        if not worker_id:
            raise ValueError("worker_id 不能为空")
    now = time.time()
    message = str(error).strip() or "unknown delivery error"
    with _transaction(write=True) as conn:
        claim_clause = " AND claim_owner = ?" if worker_id is not None else ""
        select_params: list[object] = [match_id, map_key, group_id]
        if worker_id is not None:
            select_params.append(worker_id)
        current = conn.execute(
            f"""
            SELECT attempts, status FROM deliveries
            WHERE match_id = ? AND map_key = ? AND group_id = ?
              {claim_clause}
            """,
            select_params,
        ).fetchone()
        if not current or current["status"] not in ("pending", "retry"):
            return None
        attempts = int(current["attempts"]) + 1
        final = dead or (max_attempts is not None and attempts >= max_attempts)
        status: DeliveryStatus = "dead" if final else "retry"
        retry_at = None if final else next_retry_at
        conn.execute(
            """
            UPDATE deliveries
            SET status = ?, attempts = ?, last_error = ?,
                next_retry_at = ?, updated_at = ?, sent_at = NULL,
                cancelled_at = NULL, claim_owner = NULL, claim_until = NULL
            WHERE match_id = ? AND map_key = ? AND group_id = ?
            """,
            (
                status,
                attempts,
                message,
                retry_at,
                now,
                match_id,
                map_key,
                group_id,
            ),
        )
        conn.execute(
            """
            UPDATE delivery_batches SET updated_at = ?
            WHERE match_id = ? AND map_key = ?
            """,
            (now, match_id, map_key),
        )
        row = conn.execute(
            """
            SELECT * FROM deliveries
            WHERE match_id = ? AND map_key = ? AND group_id = ?
            """,
            (match_id, map_key, group_id),
        ).fetchone()
    return _delivery_from_row(row)


def mark_delivery_dead(
    match_id: str, map_key: str, group_id: int, error: object
) -> Optional[Delivery]:
    return mark_delivery_failed(match_id, map_key, group_id, error, dead=True)


def defer_delivery(
    match_id: str,
    map_key: str,
    group_id: int,
    next_retry_at: float,
    error: object,
    *,
    worker_id: Optional[str] = None,
) -> Optional[Delivery]:
    """延后投递但不消耗 attempts，适用于 bot 离线等非发送失败。"""
    match_id, map_key = _validate_delivery_key(match_id, map_key)
    group_id = _normalize_group_ids([group_id])[0]
    if worker_id is not None:
        worker_id = str(worker_id).strip()
        if not worker_id:
            raise ValueError("worker_id 不能为空")
    retry_at = float(next_retry_at)
    now = time.time()
    message = str(error).strip() or "delivery deferred"
    with _transaction(write=True) as conn:
        claim_clause = " AND claim_owner = ?" if worker_id is not None else ""
        params: list[object] = [message, retry_at, now, match_id, map_key, group_id]
        if worker_id is not None:
            params.append(worker_id)
        cursor = conn.execute(
            f"""
            UPDATE deliveries
            SET status = 'retry', last_error = ?, next_retry_at = ?,
                updated_at = ?, claim_owner = NULL, claim_until = NULL
            WHERE match_id = ? AND map_key = ? AND group_id = ?
              AND status IN ('pending', 'retry')
              {claim_clause}
            """,
            params,
        )
        if cursor.rowcount != 1:
            return None
        conn.execute(
            """
            UPDATE delivery_batches SET updated_at = ?
            WHERE match_id = ? AND map_key = ?
            """,
            (now, match_id, map_key),
        )
        row = conn.execute(
            """
            SELECT * FROM deliveries
            WHERE match_id = ? AND map_key = ? AND group_id = ?
            """,
            (match_id, map_key, group_id),
        ).fetchone()
    return _delivery_from_row(row)


def release_claim(
    match_id: str,
    map_key: str,
    group_id: int,
    *,
    worker_id: str,
) -> bool:
    """释放单条投递上的 lease。

    用于 worker 跳过已退订群、或永久失败路径上已清理但仍占着 claim 的行，
    避免等满 lease(默认 300s) 才能被再次领取。
    """
    match_id, map_key = _validate_delivery_key(match_id, map_key)
    group_id = _normalize_group_ids([group_id])[0]
    worker_id = str(worker_id).strip()
    if not worker_id:
        raise ValueError("worker_id 不能为空")
    with _transaction(write=True) as conn:
        cursor = conn.execute(
            """
            UPDATE deliveries SET claim_owner = NULL, claim_until = NULL
            WHERE match_id = ? AND map_key = ? AND group_id = ?
              AND claim_owner = ? AND status IN ('pending', 'retry')
            """,
            (match_id, map_key, group_id, worker_id),
        )
        return cursor.rowcount == 1


def release_claims(worker_id: str) -> int:
    """释放 worker 所有活跃 lease，供 consumer 优雅退出的 finally 调用。"""
    worker_id = str(worker_id).strip()
    if not worker_id:
        raise ValueError("worker_id 不能为空")
    with _transaction(write=True) as conn:
        cursor = conn.execute(
            """
            UPDATE deliveries SET claim_owner = NULL, claim_until = NULL
            WHERE claim_owner = ? AND status IN ('pending', 'retry')
            """,
            (worker_id,),
        )
        return cursor.rowcount


def replay_dead(
    match_id: Optional[str] = None,
    map_key: Optional[str] = None,
    group_id: Optional[int] = None,
    *,
    next_retry_at: Optional[float] = None,
) -> int:
    """将筛选到的 dead 重置为 retry，清零 attempts 并返回行数。"""
    clauses = ["status = 'dead'"]
    params: list[object] = []
    if match_id is not None:
        match_id = str(match_id).strip()
        if not match_id:
            raise ValueError("match_id 不能为空")
        clauses.append("match_id = ?")
        params.append(match_id)
    if map_key is not None:
        map_key = str(map_key).strip()
        if not map_key:
            raise ValueError("map_key 不能为空")
        clauses.append("map_key = ?")
        params.append(map_key)
    if group_id is not None:
        group_id = _normalize_group_ids([group_id])[0]
        clauses.append("group_id = ?")
        params.append(group_id)
    now = time.time()
    retry_at = now if next_retry_at is None else float(next_retry_at)
    where = " AND ".join(clauses)
    with _transaction(write=True) as conn:
        batches = conn.execute(
            f"SELECT DISTINCT match_id, map_key FROM deliveries WHERE {where}",
            params,
        ).fetchall()
        cursor = conn.execute(
            f"""
            UPDATE deliveries
            SET status = 'retry', attempts = 0, next_retry_at = ?,
                updated_at = ?, sent_at = NULL, cancelled_at = NULL,
                claim_owner = NULL, claim_until = NULL
            WHERE {where}
            """,
            [retry_at, now, *params],
        )
        conn.executemany(
            """
            UPDATE delivery_batches SET updated_at = ?
            WHERE match_id = ? AND map_key = ?
            """,
            ((now, row["match_id"], row["map_key"]) for row in batches),
        )
        return cursor.rowcount


def delivery_complete(match_id: str, map_key: str) -> bool:
    """批次存在且所有行均为 sent/cancelled 时返回 True。

    dead 需要管理员明确 replay，不自动视为成功或完成。
    """
    match_id, map_key = _validate_delivery_key(match_id, map_key)
    with _transaction() as conn:
        batch = conn.execute(
            """
            SELECT 1 FROM delivery_batches WHERE match_id = ? AND map_key = ?
            """,
            (match_id, map_key),
        ).fetchone()
        if not batch:
            return False
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM deliveries
            WHERE match_id = ? AND map_key = ?
              AND status NOT IN ('sent', 'cancelled')
            """,
            (match_id, map_key),
        ).fetchone()
    return int(row["n"]) == 0


def delivery_all_sent(match_id: str, map_key: str) -> bool:
    """批次存在且所有行均为 sent 时返回 True。"""
    match_id, map_key = _validate_delivery_key(match_id, map_key)
    with _transaction() as conn:
        batch = conn.execute(
            """
            SELECT 1 FROM delivery_batches WHERE match_id = ? AND map_key = ?
            """,
            (match_id, map_key),
        ).fetchone()
        if not batch:
            return False
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM deliveries
            WHERE match_id = ? AND map_key = ? AND status != 'sent'
            """,
            (match_id, map_key),
        ).fetchone()
    return int(row["n"]) == 0


def delivery_summary(match_id: str, map_key: str) -> dict[str, int]:
    """返回各状态数量，便于状态页和告警。"""
    match_id, map_key = _validate_delivery_key(match_id, map_key)
    summary = {"pending": 0, "sent": 0, "retry": 0, "dead": 0, "cancelled": 0}
    with _transaction() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS n FROM deliveries
            WHERE match_id = ? AND map_key = ? GROUP BY status
            """,
            (match_id, map_key),
        ).fetchall()
    for row in rows:
        summary[str(row["status"])] = int(row["n"])
    return summary


def outbox_overview(now: Optional[float] = None) -> dict[str, int]:
    """返回状态页所需的全局 outbox 计数。"""
    now = time.time() if now is None else float(now)
    overview = {
        "batches": 0,
        "payload_batches": 0,
        "payload_bytes": 0,
        "due": 0,
        "claimed": 0,
        "pending": 0,
        "sent": 0,
        "retry": 0,
        "dead": 0,
        "cancelled": 0,
    }
    with _transaction() as conn:
        batch_row = conn.execute(
            """
            SELECT COUNT(*) AS batches,
                   COUNT(payload_path) AS payload_batches,
                   COALESCE(SUM(payload_size), 0) AS payload_bytes
            FROM delivery_batches
            """
        ).fetchone()
        status_rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM deliveries GROUP BY status"
        ).fetchall()
        due_row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM deliveries AS d
            JOIN delivery_batches AS b
              ON b.match_id = d.match_id AND b.map_key = d.map_key
            WHERE b.payload_path IS NOT NULL
              AND (
                d.status = 'pending'
                OR (d.status = 'retry'
                    AND (d.next_retry_at IS NULL OR d.next_retry_at <= ?))
              )
              AND (d.claim_until IS NULL OR d.claim_until <= ?)
            """,
            (now, now),
        ).fetchone()
        claimed_row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM deliveries
            WHERE status IN ('pending', 'retry') AND claim_until > ?
            """,
            (now,),
        ).fetchone()
    overview["batches"] = int(batch_row["batches"])
    overview["payload_batches"] = int(batch_row["payload_batches"])
    overview["payload_bytes"] = int(batch_row["payload_bytes"])
    overview["due"] = int(due_row["n"])
    overview["claimed"] = int(claimed_row["n"])
    for row in status_rows:
        overview[str(row["status"])] = int(row["n"])
    return overview


def prune_delivery_batches(older_than_days: float, now: Optional[float] = None) -> int:
    """线程安全地执行 outbox retention cleanup。"""
    with _JSON_LOCK:
        return _prune_delivery_batches_locked(older_than_days, now)


def _prune_delivery_batches_locked(older_than_days: float, now: Optional[float] = None) -> int:
    """删除超龄且仅含 sent/cancelled 的批次及 payload。

    dead、pending 和 retry 批次无论多旧都保留。
    """
    if older_than_days < 0:
        raise ValueError("older_than_days 不能为负数")
    now = time.time() if now is None else float(now)
    cutoff = now - float(older_than_days) * 86400
    with _transaction(write=True) as conn:
        rows = conn.execute(
            """
            SELECT b.match_id, b.map_key, b.payload_path
            FROM delivery_batches AS b
            WHERE b.updated_at < ?
              AND NOT EXISTS (
                SELECT 1 FROM deliveries AS d
                WHERE d.match_id = b.match_id AND d.map_key = b.map_key
                  AND d.status NOT IN ('sent', 'cancelled')
              )
            """,
            (cutoff,),
        ).fetchall()
        conn.executemany(
            """
            DELETE FROM delivery_batches WHERE match_id = ? AND map_key = ?
            """,
            ((row["match_id"], row["map_key"]) for row in rows),
        )
    for row in rows:
        if not row["payload_path"]:
            continue
        try:
            _safe_outbox_path(str(row["payload_path"])).unlink(missing_ok=True)
        except (OSError, PersistenceError) as e:
            logger.warning(f"[cs2] 清理 payload 失败: {e}")
    return len(rows)


_initialize_database()
try:
    migrate_legacy_subscriptions()
except PersistenceError as e:
    # 不吞掉 migration 状态：marker 未写入，修复旧文件后下次
    # 启动会再试；已存在的 DB 订阅仍可继续工作。
    logger.error(f"[cs2] 旧订阅迁移失败，下次启动将重试: {e}")


# ——————————————————— 顶级赛事白名单(带 TTL)———————————————————
# 结构: { event_id: {"slug": str, "name": str, "last_seen": epoch} }
_whitelist: dict[str, dict] = _load(_WL, {})


def update_whitelist(events: list[tuple[str, str, str]]) -> None:
    """events: [(event_id, slug, name)];刷新 last_seen。"""
    now = time.time()
    with _JSON_LOCK:
        updated = dict(_whitelist)
        for eid, slug, name in events:
            updated[eid] = {"slug": slug, "name": name, "last_seen": now}
        _dump(_WL, updated)
        _whitelist.clear()
        _whitelist.update(updated)


def prune_whitelist(sticky_days: int) -> None:
    cutoff = time.time() - sticky_days * 86400
    with _JSON_LOCK:
        dead = [eid for eid, v in _whitelist.items() if v.get("last_seen", 0) < cutoff]
        if dead:
            updated = {eid: v for eid, v in _whitelist.items() if eid not in dead}
            _dump(_WL, updated)
            _whitelist.clear()
            _whitelist.update(updated)


def whitelist_event_ids() -> set[str]:
    with _JSON_LOCK:
        return set(_whitelist.keys())


def whitelist_slugs() -> set[str]:
    with _JSON_LOCK:
        return {v.get("slug", "") for v in _whitelist.values() if v.get("slug")}


def whitelist_view() -> dict[str, dict]:
    with _JSON_LOCK:
        return {key: dict(value) for key, value in _whitelist.items()}


# ——————————————————— 已推送去重((match, map))———————————————————
# 结构: { match_id: [mapstatsid, ...] }
_pushed: dict[str, list[str]] = _load(_PUSHED, {})


def already_pushed(match_id: str, map_key: str) -> bool:
    with _JSON_LOCK:
        return map_key in _pushed.get(match_id, [])


def mark_pushed(match_id: str, map_key: str) -> None:
    with _JSON_LOCK:
        if map_key in _pushed.get(match_id, []):
            return
        updated = {key: list(value) for key, value in _pushed.items()}
        updated.setdefault(match_id, []).append(map_key)
        _dump(_PUSHED, updated)
        _pushed.clear()
        _pushed.update(updated)


def forget_old_pushed(keep: int = 400) -> None:
    """防止 pushed.json 无限膨胀:只保留最近的若干场比赛。"""
    if keep < 0:
        raise ValueError("keep 不能为负数")
    with _JSON_LOCK:
        if len(_pushed) > keep:
            keys = list(_pushed.keys())[-keep:] if keep else []
            updated = {key: list(_pushed[key]) for key in keys}
            _dump(_PUSHED, updated)
            _pushed.clear()
            _pushed.update(updated)


# ——————————— 已完整处理的比赛(补报 scan_backstop 的去重)———————————
_DONE = DATA_DIR / "results_done.json"
_done: list[str] = _load(_DONE, [])


def is_done(match_id: str) -> bool:
    with _JSON_LOCK:
        return match_id in _done


def mark_done(match_id: str, keep: int = 500) -> None:
    if keep <= 0:
        raise ValueError("keep 必须大于 0")
    with _JSON_LOCK:
        if match_id in _done:
            return
        updated = [*_done, match_id][-keep:]
        _dump(_DONE, updated)
        _done[:] = updated


# ————————————— 赛事 → 方形 eventlogo URL 映射(懒发现) —————————————
# listing 页只有横幅,方形 logo 在各赛事页里;抓一次赛事页发现 URL 后存这里,免重复抓页。
# 结构: { event_id: {"url": str, "ts": epoch} };旧版纯字符串值在加载时就地迁移。
_event_logos: dict[str, dict] = {
    k: (v if isinstance(v, dict) else {"url": v, "ts": time.time()})
    for k, v in _load(_EVLOGO, {}).items()
}


def get_event_logo_url(event_id: str) -> Optional[str]:
    with _JSON_LOCK:
        v = _event_logos.get(event_id)
        return v.get("url") if v else None


def set_event_logo_url(event_id: str, url: str) -> None:
    if not url:
        return
    with _JSON_LOCK:
        current = _event_logos.get(event_id)
        if current and current.get("url") == url:
            return
        updated = {key: dict(value) for key, value in _event_logos.items()}
        updated[event_id] = {"url": url, "ts": time.time()}
        _dump(_EVLOGO, updated)
        _event_logos.clear()
        _event_logos.update(updated)


def prune_event_logos(keep_days: int = 180) -> int:
    """清掉太久没更新、且已不在白名单里的赛事 logo 映射(重现的赛事会懒发现回来)。"""
    cutoff = time.time() - keep_days * 86400
    keep = whitelist_event_ids()
    with _JSON_LOCK:
        dead = [
            key
            for key, value in _event_logos.items()
            if key not in keep and value.get("ts", 0) < cutoff
        ]
        if dead:
            updated = {key: dict(value) for key, value in _event_logos.items() if key not in dead}
            _dump(_EVLOGO, updated)
            _event_logos.clear()
            _event_logos.update(updated)
        return len(dead)


# ———————————————————————— logo 缓存 ————————————————————————
def logo_key(url: str) -> Optional[str]:
    """从 img-cdn URL 取内容哈希做文件名,如 teamlogo_9vOlYp2U…

    坑:HLTV 同一队标的 day/night 两个变体常**共用同一路径哈希**,只靠 `&invert=true`
    查询参数区分(白色队标的深色版就是 day+invert=true)。若只取路径哈希,两变体会
    落到同一个缓存文件、互相覆盖 → 取深色版失败。故 invert=true 的加 `_i` 后缀区分。"""
    m = re.search(r"/(teamlogo|eventlogo)/([^/.?&]+)", url)
    if not m:
        return None
    suffix = "_i" if "invert=true" in (url or "") else ""
    return f"{m.group(1)}_{m.group(2)}{suffix}"


def _logo_path(url: str) -> Optional[Path]:
    k = logo_key(url)
    return (LOGO_DIR / f"{k}.img") if k else None


def has_logo(url: str) -> bool:
    p = _logo_path(url)
    return bool(p and p.exists())


def save_logo(url: str, data: bytes) -> None:
    p = _logo_path(url)
    if p and data:
        try:
            p.write_bytes(data)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cs2] 保存 logo 失败 {url}: {e}")
            return
        # 字节变了,已编码的 data URI 作废(见 _LOGO_URI_CACHE)
        k = logo_key(url)
        if k:
            _LOGO_URI_CACHE.pop(k, None)


def _sniff_mime(data: bytes) -> str:
    """按文件头判 MIME。HLTV 队标混用 webp / png / **svg**(很多队是矢量图),
    早期只分 png/webp 会把 svg 误标成 webp → 浏览器解不了 → 破图。"""
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    head = data[:256].lstrip().lstrip(b"\xef\xbb\xbf").lstrip()  # 去 BOM/空白
    if head[:5].lower() == b"<?xml" or head[:4].lower() == b"<svg" or b"<svg" in head.lower():
        return "image/svg+xml"
    return "image/webp"  # 兜底(HLTV 位图默认 webp)


# 已编码的 logo data URI 缓存。一张卡里同一支队的队标会被 emit 8~200 次
# (地图 chip、pick 标、评分表、VRS 面板、瑞士轮每个对阵……),不缓存的话每次都要
# 重新 read_bytes + 嗅探 MIME + base64,还附带一次 os.utime 的 inode 写。
# key = logo_key(url)(只认路径哈希,不含 imgix 签名,故签名轮换不影响命中)。
_LOGO_URI_CACHE: dict[str, str] = {}
_LOGO_URI_MAX = 512
# 本轮渲染已 touch 过的 logo,避免同一张卡里对同一文件反复 utime
_LOGO_TOUCHED: set[str] = set()


def logo_data_uri(url: Optional[str]) -> Optional[str]:
    """返回可直接塞进 <img src> 的 base64 data URI;没有则 None。"""
    if not url:
        return None
    k = logo_key(url)
    if not k:
        return None
    cached = _LOGO_URI_CACHE.get(k)
    if cached is not None:
        _touch_logo(k)
        return cached
    p = _logo_path(url)
    if not (p and p.exists()):
        return None
    try:
        data = p.read_bytes()
    except Exception:  # noqa: BLE001
        return None
    if not data:
        return None
    mime = _sniff_mime(data)
    uri = f"data:{mime};base64," + base64.b64encode(data).decode()
    if len(_LOGO_URI_CACHE) >= _LOGO_URI_MAX:
        _LOGO_URI_CACHE.clear()  # 容量兜底:整体重建比维护 LRU 更省事,重建只是几次读盘
    _LOGO_URI_CACHE[k] = uri
    _touch_logo(k)
    return uri


def _touch_logo(key: str) -> None:
    """记录"最近一次被渲染使用",供 prune_logos 按 mtime 清理。

    每张卡对同一 key 只 utime 一次(_LOGO_TOUCHED 去重),渲染结束由
    ``end_render_touches`` 清空——mtime 只需精确到"哪天用过",不必每次 emit 都写。
    """
    if key in _LOGO_TOUCHED:
        return
    _LOGO_TOUCHED.add(key)
    p = LOGO_DIR / f"{key}.img"
    try:
        os.utime(p)
    except OSError:
        pass


def end_render_touches() -> None:
    """一张卡渲染完毕,重置 utime 去重集合(下张卡会重新 touch 用到的 logo)。"""
    _LOGO_TOUCHED.clear()


def prune_logos(keep_days: int) -> int:
    """删掉超过 keep_days 没被渲染用到的 logo 字节(URL 含内容哈希,删错了也能按需重抓)。"""
    cutoff = time.time() - keep_days * 86400
    n = 0
    for p in LOGO_DIR.glob("*.img"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                _LOGO_URI_CACHE.pop(p.stem, None)  # 文件没了,别再从内存发旧字节
                n += 1
        except OSError:
            pass
    return n


# ———————————————————— 页面缓存(HTML,带 TTL)————————————————————
# fetcher 每次成功抓页都回写这里;命令处理器在 TTL 内直接复用,免去节流下的漫长等待。
# 内存 LRU 挡在磁盘 JSON 前:高频键(/matches、直播页)不必每次 parse 整文件。
_PAGE_MEM: OrderedDict[str, tuple[float, str]] = OrderedDict()
_PAGE_MEM_MAX = 48
_PAGE_MEM_LOCK = threading.Lock()


def _page_stem(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:24]


def _page_path(key: str) -> Path:
    """页面缓存正文:直接存**裸 HTML**,写入时间就是文件 mtime。

    早先是 ``{"key","ts","data"}`` 的 JSON 信封,但整页 HTML 要被 JSON 转义再反转义
    (1MB 的页面光 json.dump 就 ~4ms),而 ``key`` 字段从来没被读回去过、``ts`` 用
    mtime 就够。裸文件省掉这一进一出。
    """
    return CACHE_DIR / (_page_stem(key) + ".html")


def _cache_path(key: str) -> Path:
    """旧版 JSON 信封路径。只读不写,留给升级前落下的缓存文件自然过期。"""
    return CACHE_DIR / (_page_stem(key) + ".json")


def _write_page(path: Path, data: str) -> None:
    """临时文件 + 原子替换写入页面缓存。

    **不 fsync**:这是可丢弃的 HLTV 页面快照,掉电丢了下次重抓即可,不值得为它
    在事件循环里付两次 fsync(整页 1~2MB)。原子替换仍保留,避免读到半个文件。
    """
    tmp_path: Optional[Path] = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp_path, path)
    except Exception as e:  # noqa: BLE001 — 缓存写失败不该影响主流程
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        logger.warning(f"[cs2] 写入页面缓存失败 {path.name}: {e}")


def _page_mem_get(key: str, max_age: float, now: float) -> Optional[tuple[float, str]]:
    with _PAGE_MEM_LOCK:
        hit = _PAGE_MEM.get(key)
        if not hit:
            return None
        ts, data = hit
        if now - ts > max_age:
            # 过期项直接扔掉,别让它一直占着内存(整页 HTML 动辄 1MB)
            del _PAGE_MEM[key]
            return None
        _PAGE_MEM.move_to_end(key)
        return ts, data


def _page_mem_put(key: str, ts: float, data: str) -> None:
    if not data:
        return
    with _PAGE_MEM_LOCK:
        _PAGE_MEM[key] = (ts, data)
        _PAGE_MEM.move_to_end(key)
        while len(_PAGE_MEM) > _PAGE_MEM_MAX:
            _PAGE_MEM.popitem(last=False)


def cache_get_with_ts(key: str, max_age: float) -> Optional[tuple[float, str]]:
    """同 cache_get,但连这份副本的写入时间一起返回。

    时间戳是「这份 HTML 的版本号」:调用方可以用 ``(url, ts)`` 当键缓存**解析结果**,
    页面没换就不必重解析(比赛页解析一次 ~27ms)。
    """
    if max_age <= 0:
        return None
    now = time.time()
    mem = _page_mem_get(key, max_age, now)
    if mem is not None:
        return mem

    path = _page_path(key)
    try:
        ts = path.stat().st_mtime
        if now - ts <= max_age:
            data = path.read_text("utf-8")
            if data:
                _page_mem_put(key, ts, data)
                return ts, data
            return None
    except OSError:
        pass  # 没有裸文件 → 试旧版 JSON 信封

    obj = _load(_cache_path(key), None)
    if not obj or now - obj.get("ts", 0) > max_age:
        return None
    data = obj.get("data") or None
    if not data:
        return None
    ts = float(obj.get("ts", now))
    _page_mem_put(key, ts, data)
    return ts, data


def cache_get(key: str, max_age: float) -> Optional[str]:
    hit = cache_get_with_ts(key, max_age)
    return hit[1] if hit else None


def cache_set_mem(key: str, data: str) -> None:
    """只写内存副本。给"同步入内存、落盘丢线程"的异步调用方用(见 fetcher)。"""
    if data:
        _page_mem_put(key, time.time(), data)


def cache_write_disk(key: str, data: str) -> None:
    """把页面写到磁盘。整页 1~2MB,**应当在线程里调**,别堵事件循环。"""
    if data:
        _write_page(_page_path(key), data)


def cache_set(key: str, data: str) -> None:
    """同步版:内存 + 磁盘一起写。"""
    if data:
        cache_set_mem(key, data)
        cache_write_disk(key, data)


def _page_files() -> Iterator[Path]:
    """当前的裸 HTML 缓存 + 升级前落下的旧 JSON 信封。"""
    with os.scandir(CACHE_DIR) as it:
        for entry in it:
            if entry.is_file() and entry.name.endswith((".html", ".json")):
                yield Path(entry.path)


def prune_page_cache(max_age: float = 86400) -> int:
    n = 0
    cutoff = time.time() - max_age
    for p in _page_files():
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                n += 1
        except OSError:
            pass
    return n


def cache_overview() -> dict:
    """给 /cs2 状态 用的缓存概览。"""
    # scandir 的 dirent 自带 stat,不必对每个文件再 stat 一次(200+ 个文件时差别明显)
    logos = pages = size = 0
    for directory, suffixes, is_logo in ((LOGO_DIR, (".img",), True), (CACHE_DIR, (".html", ".json"), False)):
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    if not (entry.is_file() and entry.name.endswith(suffixes)):
                        continue
                    if is_logo:
                        logos += 1
                    else:
                        pages += 1
                    try:
                        size += entry.stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return {"logos": logos, "pages": pages, "kb": size // 1024}
