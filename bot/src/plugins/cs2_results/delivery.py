"""OneBot outbox consumer.

Delivery is intentionally independent from HLTV tracking. Once a rendered PNG is persisted in
the outbox, this worker can continue retrying after the match has ended or the process restarted.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from typing import Literal

from nonebot import get_bot
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.log import logger

from . import store
from .config import Config

# NapCat/QQNT EventRet.result codes observed when the group itself refuses the send
# (全员禁言、机器人被禁言、群被封等). Empty errMsg is common; do not alert admins.
_NT_GROUP_BLOCK_RESULTS = frozenset({120})

# Permanent group-side failures: no point retrying.
_PERMANENT_GROUP_MARKERS = (
    "群已解散",
    "群不存在",
    "群聊不存在",
    "不在群",
    "不是群成员",
    "被踢",
    "已退群",
    "group not found",
    "not in group",
    "kicked",
)

# Temporary / semi-temporary group-side blocks (mute, platform ban of group, etc.).
_TEMPORARY_GROUP_MARKERS = (
    "禁言",
    "全员禁言",
    "被禁",
    "封禁",
    "群被封",
    "mute",
    "shutup",
    "shut up",
    "banned",
)

_NT_RESULT_RE = re.compile(r'"result"\s*:\s*(-?\d+)')


SendFailureKind = Literal["transient", "temporary_group", "permanent_group"]


def classify_send_failure(exc: BaseException) -> SendFailureKind:
    """Classify a send_group_msg failure for retry/alert policy.

    - ``permanent_group``: group gone / bot not a member — dead immediately, no admin alert
    - ``temporary_group``: 禁言 / 群被封 / NT result 120 — 给该群记一个静默期,卡片顺延到
      解禁再发,**不消耗重试次数**、不告警(见 ``DeliveryWorker._park_muted``)。重试没有
      意义:全员禁言会一直挡着,再试还是 result 120。
    - ``transient``: infra / media / unknown — retry and alert if it becomes a dead letter
    """
    text = str(exc)
    lower = text.lower()
    if any(marker in text or marker in lower for marker in _PERMANENT_GROUP_MARKERS):
        return "permanent_group"
    if any(marker in text or marker in lower for marker in _TEMPORARY_GROUP_MARKERS):
        return "temporary_group"
    match = _NT_RESULT_RE.search(text)
    if match and int(match.group(1)) in _NT_GROUP_BLOCK_RESULTS:
        return "temporary_group"
    return "transient"


@dataclass(frozen=True, slots=True)
class DeliveryRun:
    claimed: int = 0
    sent: int = 0
    retried: int = 0
    deferred: int = 0
    # Unexpected dead letters that should page operators (payload corrupt, unknown API errors).
    dead: int = 0
    # Group-side blocks (mute/ban); still terminal, but do not alert admins.
    dead_expected: int = 0
    # Groups auto-removed because they no longer exist / bot is not a member.
    unsubscribed: int = 0
    # Leases released when skipping non-subscribed groups (no send attempt).
    released: int = 0


def drop_unreachable_subscription(group_id: int, *, reason: str) -> bool:
    """Remove a dead group from the subscription list and cancel its active deliveries.

    Returns True when the group was still subscribed (a real list change).
    Safe to call repeatedly; second call is a no-op.
    """
    removed = store.unsubscribe(group_id)
    if removed:
        logger.warning(
            f"[cs2] 自动退订群 {group_id}: {reason[:200]}"
        )
    return removed


class DeliveryWorker:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._worker_id = f"qqbot-{uuid.uuid4().hex[:12]}"
        # group_id -> 静默期结束的 epoch 秒。撞上禁言就记一笔,期间不再对该群发消息。
        self._muted_until: dict[int, float] = {}

    def release_claims(self) -> int:
        """Release leases owned by this process during graceful shutdown."""
        return store.release_claims(self._worker_id)

    def note_group_muted(self, group_id: int, *, seconds: float | None = None) -> float:
        """标记某群处于禁言中,返回静默期结束时刻。

        ``seconds`` 为 None 时用配置的默认静默期。收到 OneBot 的禁言通知时可以直接
        按通知里的 ``duration`` 传进来;全员禁言的 duration 为 0(无限期),这种情况
        仍退回默认静默期 —— 反正解禁会有通知,到时立刻清掉。
        """
        window = self._cfg.cs2_mute_backoff_minutes * 60.0
        if seconds and seconds > 0:
            window = min(max(float(seconds), 60.0), 24 * 3600.0)
        until = time.time() + window
        self._muted_until[group_id] = max(self._muted_until.get(group_id, 0.0), until)
        return self._muted_until[group_id]

    def clear_group_mute(self, group_id: int) -> bool:
        """解禁:立刻结束静默期,让顺延中的战报下一轮就发出去。"""
        return self._muted_until.pop(group_id, None) is not None

    def muted_until(self, group_id: int) -> float:
        """该群静默期结束时刻;不在静默期返回 0。顺带清理已过期的条目。"""
        until = self._muted_until.get(group_id, 0.0)
        if until and until <= time.time():
            self._muted_until.pop(group_id, None)
            return 0.0
        return until

    def _park_muted(self, delivery: store.Delivery, mute_end: float) -> bool:
        """禁言期间挂起一张卡。返回 True=已顺延,False=太旧已丢弃。"""
        max_age = self._cfg.cs2_mute_defer_max_hours * 3600
        if time.time() - delivery.created_at > max_age:
            store.mark_delivery_failed(
                delivery.match_id,
                delivery.map_key,
                delivery.group_id,
                f"群 {delivery.group_id} 持续禁言超过 "
                f"{self._cfg.cs2_mute_defer_max_hours:g} 小时,战报已过时",
                dead=True,
                worker_id=self._worker_id,
            )
            logger.info(
                f"[cs2] 群 {delivery.group_id} 仍在禁言,战报已过时丢弃:"
                f"{delivery.match_id}/{delivery.map_key}"
            )
            return False
        store.defer_delivery(
            delivery.match_id,
            delivery.map_key,
            delivery.group_id,
            mute_end,
            f"群 {delivery.group_id} 禁言中,等待解禁后投递",
            worker_id=self._worker_id,
        )
        return True

    async def run_once(self, *, limit: int = 100) -> DeliveryRun:
        deliveries = store.claim_due_deliveries(
            self._worker_id,
            lease_seconds=300,
            limit=limit,
        )
        if not deliveries:
            return DeliveryRun()

        try:
            bot = get_bot()
        except Exception as exc:  # noqa: BLE001
            retry_at = time.time() + self._cfg.cs2_delivery_retry_base_seconds
            for delivery in deliveries:
                store.defer_delivery(
                    delivery.match_id,
                    delivery.map_key,
                    delivery.group_id,
                    retry_at,
                    f"OneBot unavailable: {exc}"[:1000],
                    worker_id=self._worker_id,
                )
            return DeliveryRun(claimed=len(deliveries), deferred=len(deliveries))

        sent = retried = deferred = dead = dead_expected = unsubscribed = 0
        released = 0
        muted_this_run: set[int] = set()  # 每群每轮只播报一次禁言,别按卡刷屏
        active_groups = store.get_subscriptions()
        payloads: dict[tuple[str, str], bytes | None] = {}
        payload_errors: dict[tuple[str, str], str] = {}
        segments: dict[tuple[str, str], MessageSegment] = {}
        for delivery in deliveries:
            if delivery.group_id not in active_groups:
                # unsubscribe() normally cancels the durable row; a stale claim snapshot
                # may still hold the lease. Always release so peers can reclaim immediately.
                if store.release_claim(
                    delivery.match_id,
                    delivery.map_key,
                    delivery.group_id,
                    worker_id=self._worker_id,
                ):
                    released += 1
                continue
            # 该群正处于禁言静默期:不必再试(试也是 result 120),直接顺延到解禁,
            # 且**不消耗重试次数** —— 解禁后这张卡还能发出去。放在读取图片之前,
            # 省掉整张卡的读盘。
            mute_end = self.muted_until(delivery.group_id)
            if mute_end:
                if self._park_muted(delivery, mute_end):
                    deferred += 1
                else:
                    dead_expected += 1
                continue

            key = (delivery.match_id, delivery.map_key)
            if key not in payloads:
                try:
                    payloads[key] = store.get_delivery_payload(*key)
                except Exception as exc:  # noqa: BLE001
                    payloads[key] = None
                    payload_errors[key] = f"outbox payload unreadable: {exc}"[:1000]
            payload = payloads[key]
            if not payload:
                if key in payload_errors:
                    updated = store.mark_delivery_failed(
                        delivery.match_id,
                        delivery.map_key,
                        delivery.group_id,
                        payload_errors[key],
                        dead=True,
                        worker_id=self._worker_id,
                    )
                    if updated:
                        dead += 1
                    continue
                store.defer_delivery(
                    delivery.match_id,
                    delivery.map_key,
                    delivery.group_id,
                    time.time() + 60,
                    "outbox payload missing",
                    worker_id=self._worker_id,
                )
                deferred += 1
                continue
            try:
                segment = segments.get(key)
                if segment is None:
                    segment = MessageSegment.image(payload)
                    segments[key] = segment
                if delivery.mentions:
                    # 个人订阅命中:图片前逐个 @ 订阅者(只有在群成员才会真响铃)。
                    msg = Message(MessageSegment.at(qq) for qq in delivery.mentions)
                    msg.append(segment)
                    await bot.send_group_msg(group_id=delivery.group_id, message=msg)
                else:
                    await bot.send_group_msg(group_id=delivery.group_id, message=segment)
            except Exception as exc:  # noqa: BLE001
                kind = classify_send_failure(exc)
                err_text = str(exc)[:1000]
                if kind == "permanent_group":
                    # Group dissolved / bot kicked / not a member: drop subscription so we
                    # stop enqueueing work. unsubscribe() also cancels pending/retry rows
                    # for this group (including the current claimed one).
                    if drop_unreachable_subscription(
                        delivery.group_id,
                        reason=f"投递永久失败 {delivery.match_id}/{delivery.map_key}: {err_text}",
                    ):
                        unsubscribed += 1
                    else:
                        # Already unsubscribed (or never listed): still free the lease.
                        store.release_claim(
                            delivery.match_id,
                            delivery.map_key,
                            delivery.group_id,
                            worker_id=self._worker_id,
                        )
                    active_groups.discard(delivery.group_id)
                    continue

                if kind == "temporary_group":
                    # 群被禁言:重试没有意义(全员禁言会一直挡着,再试还是 result 120),
                    # 而且原来那套指数退避会让**每张卡**都空跑 5 次再假死信。改成给这个群
                    # 记一个静默期,本张卡顺延到解禁,不消耗重试次数。
                    mute_end = self.note_group_muted(delivery.group_id)
                    if delivery.group_id not in muted_this_run:
                        muted_this_run.add(delivery.group_id)
                        logger.info(
                            f"[cs2] 群 {delivery.group_id} 处于禁言中,"
                            f"暂停推送 {self._cfg.cs2_mute_backoff_minutes} 分钟"
                            f"(解禁通知会提前恢复)"
                        )
                    if self._park_muted(delivery, mute_end):
                        deferred += 1
                    else:
                        dead_expected += 1
                    continue

                delay = self._cfg.cs2_delivery_retry_base_seconds * (2 ** min(delivery.attempts, 8))
                updated = store.mark_delivery_failed(
                    delivery.match_id,
                    delivery.map_key,
                    delivery.group_id,
                    err_text,
                    next_retry_at=time.time() + delay,
                    max_attempts=self._cfg.cs2_delivery_max_attempts,
                    worker_id=self._worker_id,
                )
                if updated and updated.status == "dead":
                    dead += 1
                    logger.error(
                        f"[cs2] 投递进入死信:{delivery.match_id}/{delivery.map_key}"
                        f" → 群 {delivery.group_id}: {err_text[:200]}"
                    )
                else:
                    retried += 1
            else:
                updated = store.mark_delivery_sent(
                    delivery.match_id,
                    delivery.map_key,
                    delivery.group_id,
                    worker_id=self._worker_id,
                )
                if updated:
                    sent += 1

        return DeliveryRun(
            claimed=len(deliveries),
            sent=sent,
            retried=retried,
            deferred=deferred,
            dead=dead,
            dead_expected=dead_expected,
            unsubscribed=unsubscribed,
            released=released,
        )
