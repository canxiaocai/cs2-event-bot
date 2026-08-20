"""cs2_results:在指定 QQ 群里,逐张小图播报 HLTV 顶级赛事(featured)的 CS2 战报。

三级轮询(自适应,只有订阅群存在时才请求 HLTV):
- 每天刷新顶级赛事白名单(/events 的 #FEATURED ∪ .big-event)
- 每几分钟扫 /matches,发现白名单赛事的直播 → 加入追踪
- 追踪中的比赛每 1-2 分钟抓比赛页,某张图打完就推一张战报卡

命令(COMMAND_START 见 .env,/cs2 或 cs2 均可):
  /cs2                 图片菜单,展示支持的功能(管理版仅在调试群/超管私聊出现)
  /cs2 订阅 / 退订      (群主/管理员/超管,群内)把本群加入/移出推送
  /cs2 订阅 战队|选手 <名字>  个人订阅(任何群成员):开赛提醒和每张地图赛果都 @ 你
  /cs2 退订 战队|选手 <名字>  移除个人订阅
  /cs2 我的订阅         查看你在本群订阅的战队/选手
  /cs2 赛事            未来 3 个月顶级赛事
  /cs2 日程            当前/下一个「比赛日」的关注赛事比赛(含赛果与直播)。比赛日按
                      时间空档聚类,不按日历日切——欧洲赛事跨午夜的末场仍算同一晚
  /cs2 赛程 [赛事名]    正在进行/即将开赛赛事的完整赛程(小组赛/淘汰赛)

管理/调试命令 —— 只在调试群(cfg.cs2_debug_groups,默认为空)或超管私聊可用,
普通群里视为未识别、落回公开帮助卡,不暴露其存在,也不泄露订阅信息:
  /cs2 状态            运行状态、白名单与订阅群信息
  /cs2 测试 [ID或URL]  立即渲染一场比赛的最新战报卡,便于测试
  /cs2 重试投递 [比赛ID] 重新激活全部或指定比赛的死信
  /cs2 刷新名录         强制刷新战队/选手本地名录(抓一次世界排行榜)
  /cs2 刷新VRS          强制刷新 Valve 世界排名总榜(抓一次 /valve-ranking/teams)
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from nonebot import get_bot, get_driver, get_plugin_config, on_command, on_notice, require
from nonebot.log import logger
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_htmlrender")
from nonebot.adapters.onebot.v11 import (  # noqa: E402
    GroupBanNoticeEvent,
    GroupDecreaseNoticeEvent,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.params import CommandArg  # noqa: E402

from . import hltv, names, store  # noqa: E402
from . import render as card  # noqa: E402
from .config import Config  # noqa: E402
from .delivery import DeliveryWorker, drop_unreachable_subscription  # noqa: E402
from .fetcher import Fetcher, FetchPriority  # noqa: E402
from .security import hltv_match_url  # noqa: E402

__plugin_meta__ = PluginMetadata(
    name="CS2 战报",
    description="HLTV 顶级赛事逐图战报推送:直播时每打完一张地图即推送战报卡,支持赛程/赛事查询与战队/选手订阅 @ 到人",
    usage=(
        "/cs2                         图片菜单\n"
        "/cs2 订阅 / 退订              (群管理)把本群加入/移出自动推送\n"
        "/cs2 订阅 战队|选手 <名字>     开赛提醒和每张地图赛果都 @ 你\n"
        "/cs2 退订 战队|选手 <名字>     移除个人订阅\n"
        "/cs2 我的订阅                 查看你在本群的订阅\n"
        "/cs2 赛事                    未来 3 个月顶级赛事\n"
        "/cs2 日程                    当前/下一个比赛日的比赛(含赛果与直播)\n"
        "/cs2 赛程 [赛事名]            正在进行/即将开赛赛事的完整赛程"
    ),
    type="application",
    homepage="https://github.com/canxiaocai/cs2-event-bot",
    config=Config,
    supported_adapters={"~onebot.v11"},
    extra={"author": "canxiaocai"},
)

cfg = get_plugin_config(Config)
fetcher: Fetcher = Fetcher(cfg)
delivery_worker = DeliveryWorker(cfg)
driver = get_driver()
CN = ZoneInfo("Asia/Shanghai")

# —— 运行时状态(不持久化)——
_followed: dict[str, hltv.LiveMatch] = {}
_pending_since: dict[str, float] = {}  # (match:mapkey) 首次见到完成但没评分的时间
_over_since: dict[str, float] = {}  # match_id → 首次判定系列赛结束的时间(收尾用)
_generation_alerted: set[str] = set()
_followed_since: dict[str, float] = {}  # match_id → 首次纳入追踪
_last_seen_live: dict[str, float] = {}  # match_id → 上次在 /matches 直播区见到
_score_fp: dict[str, tuple] = {}  # match_id → 最近一次解析的比分指纹
_score_stable: set[str] = set()  # 连续两次指纹相同 → 中盘可略退避
# 非顶级赛事、且经比赛页确认无任何订阅命中(如被订阅选手替补下场)→ 记下,scan 不再反复追
_no_recipient_matches: set[str] = set()
# 名录顺路收集去重:每场比赛只把队名/首发写一次本地名录
_roster_sighted: set[str] = set()
_stat = {
    "last_scan": 0.0,
    "last_poll": "",
    "last_featured": "",
    "fail_streak": 0,
    "last_error": "",
    "sources": {},
    "capacity_followed": 0,
}
_command_last: dict[tuple[str, int], float] = {}
_follow_last: dict[str, float] = {}
_last_backstop = 0.0
_next_scan_at = 0.0
_next_backstop_at = 0.0
_startup_backstop_pending = True  # 首次补报用更宽时间窗
_background_tasks: set[asyncio.Task] = set()
_poll_task: asyncio.Task | None = None

# 个人订阅命令里「战队 / 选手」两类的同义词
_TEAM_WORDS = {"战队", "队", "team", "队伍"}
_PLAYER_WORDS = {"选手", "队员", "player", "选⼿"}


_WEEKDAYS_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _now() -> str:
    return datetime.now(CN).strftime("%Y-%m-%d %H:%M")


def _live_busy() -> bool:
    """当前是否有正在追踪的直播。有的话,120s 节流几乎被逐图轮询占满,不应再让装饰性的
    logo 发现(每个赛事页占一个节流档)去抢额度、拖慢时效性强的战报推送。空闲期(如每日
    4 点白名单刷新时)_followed 为空,预热便能放开发现、一次填满。"""
    return bool(_followed)


def target_groups() -> set[int]:
    return store.get_subscriptions()


def _cooldown_left(event: MessageEvent) -> int:
    """群内按群、私聊按用户限流，避免公开查询并发挤占直播抓取和渲染。"""
    if cfg.cs2_command_cooldown <= 0:
        return 0
    scope = (
        ("group", event.group_id)
        if isinstance(event, GroupMessageEvent)
        else ("user", event.user_id)
    )
    now = time.monotonic()
    left = cfg.cs2_command_cooldown - (now - _command_last.get(scope, 0.0))
    if left > 0:
        return max(1, int(left + 0.999))
    _command_last[scope] = now
    # 防无界增长:只保留仍在冷却窗口附近的条目
    keep_for = max(cfg.cs2_command_cooldown * 8, 300.0)
    if len(_command_last) > 64:
        stale = [k for k, t in _command_last.items() if now - t > keep_for]
        for k in stale:
            _command_last.pop(k, None)
    return 0


# 当前小图打到这个回合数就算「随时可能结束」(13 回合制,再拿 2 分即完场)
_ENDGAME_ROUNDS = 11
# 刚开图:再怎么快也要十几分钟才打得完,没必要密集轮询
_EARLY_ROUNDS = 5


def _live_map_phase(match_id: str) -> str:
    """从上一轮已在手的比分指纹推断当前小图打到哪了(零额外抓取)。

    ``_score_fp`` 存的就是每张图的 ``(name, t1, t2, finished, mapstatsid)``,而进行中
    那张图的比分是 scorebot 注入的**实时回合比分**(见 hltv 里的坑注)。这里只拿它
    **定轮询节奏**,不参与完赛判定 —— 判错顶多多抓/少抓一次,不会误报赛果。
    """
    fp = _score_fp.get(match_id)
    if not fp:
        return "unknown"
    live = [m for m in fp[2] if not m[3] and ((m[1] or 0) or (m[2] or 0))]
    if not live:  # 图间空档 / 还没开打
        return "unknown"
    top = max(max(m[1] or 0, m[2] or 0) for m in live)
    if top >= _ENDGAME_ROUNDS:
        return "endgame"
    if top <= _EARLY_ROUNDS:
        return "early"
    return "mid"


# 各局势分到的抓取权重(越大越勤)。权重只在**并发直播之间瓜分同一份额度**,
# 不改变总请求率 —— 见 _follow_poll_interval。
_PHASE_WEIGHT = {"endgame": 2.0, "mid": 1.0, "unknown": 1.0, "early": 0.6}
_STABLE_WEIGHT = 0.8  # 比分连续未变(中盘拉锯)略退避
_FOLLOW_INTERVAL_MIN = 60.0


def _follow_weight(match_id: str) -> float:
    phase = _live_map_phase(match_id)
    weight = _PHASE_WEIGHT.get(phase, 1.0)
    if phase not in ("endgame", "early") and match_id in _score_stable:
        weight = min(weight, _STABLE_WEIGHT)
    return weight


def _follow_poll_interval(match_id: str) -> float:
    """单场追踪的自适应轮询间隔(秒)。

    - 等评分 / 系列赛已结束仍有 pending 卡 → 压到约 60s,尽快出图
    - 生成持续失败 → 至少 10 分钟低频重试
    - **按局势加权瓜分额度**:当前小图有一方 ≥11 回合(随时可能完场)权重加倍,刚开图
      (≤5 回合)权重减到 0.6。多场并发时总请求率与原来「每场 ``min_gap*(n+1)``」
      **完全一致**,只是把额度从「再怎么快也要二十分钟才打得完」的场次,挪给
      「随时可能出结果」的场次 —— 这才是真正减少播报延迟的地方。

    额度公式:令权重 w_i、并发数 n,取 ``interval_i = Σw · min_gap · (n+1) / (n · w_i)``。
    全部 w=1 时退化成原来的 ``min_gap·(n+1)``,可验算总速率 ``Σ 1/interval_i`` 不变。

    上限 ``max(600s, base)`` 是防「退避把局势看丢」:刚开图退太久,等回头再看时图
    可能已经打完,反而更慢。轮询循环是串行的(``_poll_once`` 每轮只提交一次抓取),
    真正的硬性限速是抓取层的闸门,所以这里稍微乐观一点不会造成请求堆积。
    """
    base = float(cfg.cs2_live_poll_interval * 60)
    if match_id in _generation_alerted:
        return max(base, 10 * 60)
    pending = any(k.startswith(f"{match_id}:") for k in _pending_since)
    if pending or match_id in _over_since:
        return min(base, 60.0)

    weight = _follow_weight(match_id)
    n = len(_followed)
    if n > 1:
        total = sum(_follow_weight(mid) for mid in _followed) or float(n)
        want = total * cfg.cs2_request_min_gap * (n + 1) / (n * weight)
    else:
        want = base / weight
    return max(_FOLLOW_INTERVAL_MIN, min(want, max(600.0, base)))


def _match_score_fp(match: hltv.MatchDetail) -> tuple:
    return (
        match.series1,
        match.series2,
        tuple(
            (mp.name, mp.team1_score, mp.team2_score, mp.finished, mp.mapstatsid)
            for mp in match.maps
        ),
    )


def _drop_follow(match_id: str, *, reason: str) -> None:
    """从内存追踪表移除一场比赛(不改 outbox;是否 mark_done 由调用方决定)。"""
    _followed.pop(match_id, None)
    _follow_last.pop(match_id, None)
    _followed_since.pop(match_id, None)
    _last_seen_live.pop(match_id, None)
    _score_fp.pop(match_id, None)
    _score_stable.discard(match_id)
    _over_since.pop(match_id, None)
    _generation_alerted.discard(match_id)
    _stat["sources"].pop(f"match:{match_id}", None)
    _stat["sources"].pop(f"match:{match_id}:parse", None)
    # 清掉该场 pending 评分等待
    for pk in [k for k in _pending_since if k.startswith(f"{match_id}:")]:
        _pending_since.pop(pk, None)
    logger.info(f"[cs2] 停止追踪 {match_id}: {reason}")


def _prune_stuck_follows(now: float) -> None:
    """放弃长期僵死的追踪:已离开 /matches 且超龄、无评分 pending、非 series_over 收尾。"""
    max_age = cfg.cs2_stuck_follow_hours * 3600
    for mid in list(_followed):
        age = now - _followed_since.get(mid, now)
        if age < max_age:
            continue
        if any(k.startswith(f"{mid}:") for k in _pending_since):
            continue
        if mid in _over_since and now - _over_since[mid] < 45 * 60:
            continue
        unseen = now - _last_seen_live.get(mid, 0.0)
        if unseen < min(max_age, 2 * 3600):
            continue
        _drop_follow(mid, reason=f"僵死追踪(已追踪 {age / 3600:.1f}h,离开直播列表 {unseen / 3600:.1f}h)")


def _spawn_background(coro, *, name: str) -> asyncio.Task:
    """登记后台任务，确保异常可见且进程退出时能够有序取消。"""
    task_name = f"cs2:{name}"
    existing = next(
        (task for task in _background_tasks if task.get_name() == task_name and not task.done()),
        None,
    )
    if existing is not None:
        coro.close()
        return existing
    task = asyncio.create_task(coro, name=task_name)
    _background_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if t.cancelled():
            return
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            return
        if exc:
            logger.opt(exception=exc).error(f"[cs2] 后台任务 {t.get_name()} 异常")

    task.add_done_callback(_done)
    return task


async def _run_interval(
    job: Callable[[], Awaitable[None]],
    *,
    seconds: float,
    initial_delay: float,
    name: str,
) -> None:
    await asyncio.sleep(initial_delay)
    while True:
        try:
            await job()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[cs2] 周期任务 {name} 失败: {e}")
        await asyncio.sleep(seconds)


async def _run_daily(
    job: Callable[[], Awaitable[None]], *, hour: int, minute: int, name: str
) -> None:
    while True:
        now = datetime.now(CN)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            await job()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[cs2] 每日任务 {name} 失败: {e}")


# ————————————————————————— 告警/健康 —————————————————————————
def _ok(source: str) -> None:
    sources: dict = _stat["sources"]
    state = sources.setdefault(source, {"fail_streak": 0, "last_error": ""})
    if state["fail_streak"]:
        logger.info(f"[cs2] 抓取恢复正常:{source}")
    state["fail_streak"] = 0
    _stat["fail_streak"] = max((s["fail_streak"] for s in sources.values()), default=0)


async def _fail(source: str, msg: str) -> None:
    sources: dict = _stat["sources"]
    state = sources.setdefault(source, {"fail_streak": 0, "last_error": ""})
    state["fail_streak"] += 1
    state["last_error"] = f"{_now()} {msg}"
    _stat["fail_streak"] = max(s["fail_streak"] for s in sources.values())
    _stat["last_error"] = f"{_now()} {msg}"
    logger.warning(f"[cs2] {source}:{msg}(连续失败 {state['fail_streak']})")
    if state["fail_streak"] == cfg.cs2_alert_after_failures:
        await _alert(
            f"⚠️ cs2_results[{source}] 已连续 {state['fail_streak']} 次抓取失败,"
            f"HLTV 推送可能中断。\n最近错误:{msg}"
        )


async def _alert(text: str) -> None:
    try:
        bot = get_bot()
    except Exception:  # noqa: BLE001
        return
    for uid in driver.config.superusers:
        try:
            await bot.send_private_msg(user_id=int(uid), message=text)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cs2] 告警私聊 {uid} 失败: {e}")


# ————————————————————————— 抓取/解析核心 —————————————————————————
async def refresh_whitelist() -> None:
    html = await fetcher.get_html(
        hltv.URL_EVENTS, wait_selector="#FEATURED, .events-month", priority="scan"
    )
    if not html:
        await _fail("events", "抓取失败")
        return
    _ok("events")
    events = hltv.parse_whitelist(html)
    if not events:
        await _fail("events:parse", "页面存在但未解析出任何 featured/big event")
        return
    _ok("events:parse")
    excl = set(cfg.cs2_force_exclude_events)
    tuples = [(e.id, e.slug, e.name) for e in events if e.id not in excl]
    known = {e.id for e in events}
    for eid in cfg.cs2_force_include_events:
        if eid not in known and eid not in excl:
            tuples.append((eid, "", f"event {eid}"))
    store.update_whitelist(tuples)
    store.prune_whitelist(cfg.cs2_featured_sticky_days)
    _stat["last_featured"] = _now()
    logger.info(f"[cs2] 白名单刷新完成,共 {len(store.whitelist_event_ids())} 个赛事")


async def ensure_logos(match: hltv.MatchDetail) -> None:
    need = [u for u in match.logo_urls() if not store.has_logo(u)]
    if not need:
        return
    await fetcher.get_logos(match.url, need, priority="live")  # 内部逐张落盘


def _event_logo_missing(e: hltv.EventDetail) -> bool:
    u = store.get_event_logo_url(e.id)
    return not (u and store.has_logo(u))


async def ensure_event_logos(events: list[hltv.EventDetail], cap: int = 0) -> None:
    """给赛事列表补齐方形 eventlogo,并回填到 EventDetail.logo。

    listing 页只有横幅,方形 logo 只在各赛事页里 → URL 懒发现(抓一次赛事页,
    存 event_logos.json,之后免重抓),字节按 hash 缓存。抓页较贵,给个单轮上限
    cap(默认取命令级 cs2_events_logo_fetch_cap;预热任务传更大的 cap 一次填满)。

    只在后台任务/预热里 await 调用(会被 120s 节流拖长),命令路径一律走 _bg_*。
    """
    from selectolax.parser import HTMLParser

    limit = cap or cfg.cs2_events_logo_fetch_cap
    discovered = 0
    for e in events:
        if store.get_event_logo_url(e.id):
            continue
        if discovered >= limit or _live_busy():
            # 有直播在追踪时,别用「每页一个节流档」的赛事页发现抢占直播轮询的额度,
            # 以免拖慢时效性强的单图战报推送。logo 是装饰性的,留到空闲/每日 4 点再补。
            break
        url = f"{hltv.BASE}/events/{e.id}/{e.slug or 'x'}"
        html = await fetcher.get_html(url, wait_selector="body", priority="warm")
        if not html:
            continue
        logo = hltv._pick_logo(HTMLParser(html), "eventlogo", e.name, allow_fallback=True)
        if logo:
            store.set_event_logo_url(e.id, logo)
            discovered += 1

    need = [u for e in events if (u := store.get_event_logo_url(e.id)) and not store.has_logo(u)]
    if need:
        await fetcher.get_logos(hltv.URL_EVENTS, need, priority="warm")  # 内部逐张落盘

    for e in events:
        e.logo = store.get_event_logo_url(e.id)


_ev_logo_bg = False


async def _bg_ensure_event_logos(events: list[hltv.EventDetail], cap: int = 0) -> None:
    """后台补齐赛事 logo(发现方形 URL + 抓字节),不阻塞命令。去重:同一时刻只跑一轮,
    命令被连发也只补一遍。异常只记日志——补不齐就下次再补,命令照样用首字母兜底出图。"""
    global _ev_logo_bg
    if _ev_logo_bg:
        return
    _ev_logo_bg = True
    try:
        await ensure_event_logos(events, cap=cap)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[cs2] 后台赛事 logo 补齐失败: {e}")
    finally:
        _ev_logo_bg = False


async def _prewarm_logos(attempts: int = 3, retry_delay: float = 420.0) -> None:
    """预热 logo,取不到源页面就隔一会儿重试。

    坑(2026-07-21):预热是一次性的,而启动时机很容易撞上「没网」——Mac 睡醒后
    launchd 立刻拉起 bot,此时代理/网络还没恢复,/matches 抓取 ERR_INTERNET_DISCONNECTED
    → 一张 logo 都收不到,而且要等到次日 4 点的每日任务才会再试,中间所有 /cs2 日程
    都是首字母兜底。故拿不到 /matches 就重试几轮。
    """
    for attempt in range(max(1, attempts)):
        if await _prewarm_logos_once():
            return
        if attempt + 1 < attempts:
            logger.info(f"[cs2] logo 预热没拿到源页面,{retry_delay / 60:.0f} 分钟后重试")
            await asyncio.sleep(retry_delay)


async def _prewarm_logos_once() -> bool:
    """跑一轮预热;拿到 /matches(队标的主来源)才算有效,否则返回 False 让上层重试。

    (1) 窗口内所有顶级赛事的方形 logo → /cs2 赛事 首查通常已有图;
    (2) 今日 /matches + /results 上的队标/赛事 logo → /cs2 日程 首查通常已有图。
    一律用已缓存/陈旧兜底的页面解析,尽量不额外打 HLTV;缺的 logo 走后台抓。"""
    # (1) 赛事方形 logo —— 抓页发现较贵,用预热级大 cap 一次填满,之后每天基本 0 抓
    try:
        html = await fetcher.get_html(
            hltv.URL_EVENTS,
            wait_selector="#FEATURED, .events-month",
            max_age=cfg.cs2_cache_events_ttl,
            stale_age=cfg.cs2_stale_events,
            priority="warm",
        )
        if html:
            now = time.time() * 1000
            window = now + 90 * 86400 * 1000
            evs = [
                e
                for e in hltv.parse_events(html)
                if e.start_unix and e.start_unix <= window and (e.end_unix or e.start_unix) >= now
            ]
            await ensure_event_logos(evs, cap=cfg.cs2_events_logo_prewarm_cap)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[cs2] 赛事 logo 预热失败: {e}")
    # (2) 今日队标/赛事 logo —— 从已缓存的 /matches + /results 收集,后台补齐缺失的
    try:
        need, seen = [], set()

        def _add(*us: str | None) -> None:
            for u in us:
                if u and u not in seen and not store.has_logo(u):
                    seen.add(u)
                    need.append(u)

        mhtml = await fetcher.get_html(
            hltv.URL_MATCHES,
            wait_selector=".match",
            max_age=cfg.cs2_cache_matches_ttl,
            stale_age=cfg.cs2_stale_matches,
            priority="warm",
        )
        if mhtml:
            mt = hltv.tree(mhtml)  # 建一次树,两个解析器复用(建树占单次解析 50~65%)
            for m in hltv.parse_upcoming_matches(mt):
                _add(m.event_logo, m.team1_logo, m.team2_logo)
            for m in hltv.parse_live_matches(mt):
                _add(m.event_logo, m.team1_logo, m.team2_logo)
        rhtml = await fetcher.get_html(
            hltv.URL_RESULTS,
            wait_selector=".result-con",
            max_age=cfg.cs2_cache_results_ttl,
            stale_age=cfg.cs2_stale_results,
            priority="warm",
        )
        if rhtml:
            for m in hltv.parse_results(rhtml):
                _add(m.event_logo, m.team1_logo, m.team2_logo)
        if need:
            fetcher.spawn_logos(hltv.URL_MATCHES, need, priority="warm")
        return mhtml is not None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[cs2] 队标预热失败: {e}")
        return False


async def scan_live() -> bool:
    # 短 max_age:刚被 /cs2 日程 等用户命令拉过 /matches 时直接复用,少一次导航。
    html = await fetcher.get_html(
        hltv.URL_MATCHES,
        wait_selector=".match",
        max_age=cfg.cs2_scan_cache_max_age,
        priority="scan",
    )
    if not html:
        await _fail("matches", "抓取失败")
        return False
    _ok("matches")
    wl = store.whitelist_event_ids()
    excl = set(cfg.cs2_force_exclude_events)
    # 个人订阅关注的队(显式战队 ∪ 被订阅选手当前所属队);cs2_sub_any_tier=False 时不扩展。
    watch_teams = _watch_team_keys() if cfg.cs2_sub_any_tier else set()
    now = time.time()
    for lm in hltv.parse_live_matches(html):
        top = bool(lm.event_id) and lm.event_id in wl and lm.event_id not in excl
        subbed = (
            bool(_match_team_keys(lm.team1, lm.team2) & watch_teams)
            and lm.match_id not in _no_recipient_matches
        )
        if top or subbed:
            if lm.match_id not in _followed:
                tag = "" if top else " (个人订阅)"
                logger.info(f"[cs2] 开始追踪直播:{lm.event_name} · {lm.url}{tag}")
                _followed_since[lm.match_id] = now
            _followed[lm.match_id] = lm
            _last_seen_live[lm.match_id] = now
    _prune_stuck_follows(now)
    followed_count = len(_followed)
    if followed_count > cfg.cs2_max_followed and _stat["capacity_followed"] != followed_count:
        lower_bound = int(cfg.cs2_request_min_gap * (followed_count + 1))
        logger.warning(
            f"[cs2] 同时追踪 {followed_count} 场，超过容量告警阈值 "
            f"{cfg.cs2_max_followed}；按当前节流单场轮询周期至少约 {lower_bound} 秒"
        )
    _stat["capacity_followed"] = followed_count
    return True


async def scan_backstop(now: float) -> bool:
    """补报:从 /results 找最近结束、但没被完整推送过的白名单比赛,纳入追踪走正常
    推送流程。兜住重启/离线/扫描间隙里结束的比赛(结果入库后比赛立刻离开 /matches,
    普通扫描就再也看不见它了——曾连续两次真实丢报)。/results 走页面缓存,
    idle 时每 cs2_cache_results_ttl 秒最多一次真实请求。

    进程启动后第一次补报使用 cs2_startup_backstop_window_min(默认 12h),
    之后回到 cs2_backstop_window_min。
    """
    global _startup_backstop_pending
    if not cfg.cs2_results_backstop:
        return True
    rhtml = await fetcher.get_html(
        hltv.URL_RESULTS,
        wait_selector=".result-con",
        max_age=cfg.cs2_cache_results_ttl,
        priority="scan",
    )
    if not rhtml:
        return False
    window_min = (
        cfg.cs2_startup_backstop_window_min
        if _startup_backstop_pending
        else cfg.cs2_backstop_window_min
    )
    start_ms = now * 1000 - window_min * 60 * 1000
    added = 0
    for rm in _finished_schedule_rows(rhtml, start_ms):
        mid = rm.match_id
        if mid in _followed or store.is_done(mid):
            continue
        logger.info(
            f"[cs2] 补报:{rm.team1} vs {rm.team2}({rm.event_name})已结束但无完整推送记录,纳入追踪"
        )
        _followed[mid] = hltv.LiveMatch(mid, rm.url, rm.event_id, rm.event_name)
        _followed_since.setdefault(mid, now)
        added += 1
    if _startup_backstop_pending:
        _startup_backstop_pending = False
        logger.info(
            f"[cs2] 启动宽窗口补报完成(窗口 {window_min} 分钟,新纳入 {added} 场)"
        )
    return True


async def follow_match(lm: hltv.LiveMatch, now: float) -> None:
    html = await fetcher.get_html(lm.url, wait_selector=".mapholder", priority="live")
    if not html:
        await _fail(f"match:{lm.match_id}", "比赛页抓取失败")
        return
    _ok(f"match:{lm.match_id}")
    # 整场只建一棵树:比赛页 1.1MB,建树就占 ~14ms,而下面 parse_match / parse_lineup
    # 都要用它。首发阵容再按需解析一次(~16ms)供三处共用(名录收集 / 选手命中确认 /
    # 开赛卡),此前每处各解析一遍,直播轮询每 tick 每场要白花 30~48ms。
    mtree = hltv.tree(html)
    match = hltv.parse_match(mtree, lm.url)
    _lineups: Optional[list[hltv.LineupTeam]] = None

    def lineups() -> list[hltv.LineupTeam]:
        nonlocal _lineups
        if _lineups is None:
            _lineups = hltv.parse_lineup(mtree)
        return _lineups

    if not match.maps or match.team1 == "Team 1" or match.team2 == "Team 2":
        await _fail(f"match:{lm.match_id}:parse", "比赛页结构无法识别")
        return
    _ok(f"match:{lm.match_id}:parse")
    note_match_vrs(match)  # 顺路收下两队的实时 VRS 名次(赛果一出就变,零额外请求)
    # 成功拉到比赛页也算「还活着」,避免仅依赖 /matches 列表导致误杀
    _last_seen_live[lm.match_id] = now
    _followed_since.setdefault(lm.match_id, now)

    fp = _match_score_fp(match)
    prev = _score_fp.get(lm.match_id)
    if prev is None:
        _score_fp[lm.match_id] = fp
    elif prev == fp:
        _score_stable.add(lm.match_id)
    else:
        _score_fp[lm.match_id] = fp
        _score_stable.discard(lm.match_id)

    await ensure_logos(match)

    # 名录顺路收集(每场一次,零额外抓取):两队名 + 首发 10 人(含当前所属队)并进本地名录。
    # 让转会/替补/新队自动跟上——订阅命令永远查本地,不实时打 HLTV。
    if lm.match_id not in _roster_sighted:
        _roster_sighted.add(lm.match_id)
        try:
            store.note_teams_seen([match.team1, match.team2])
            roster: list[tuple[str, str, str, str]] = []
            for lt in lineups():
                tname = lt.name or (match.team1 if lt.ordinal == 1 else match.team2)
                for pid, nick in lt.players:
                    roster.append((pid, nick, tname, names.team_key(tname)))
            store.set_player_teams(roster)  # 首发 10 人一个事务写完
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[cs2] 名录顺路收集失败: {e}")

    # 个人订阅:算出本场收件群 + 每群 @ 名单。选手命中顺路从已在手的比赛页首发解析确认
    # (零额外抓取)。无任何订阅时,顶级赛事退化为「所有火喇叭群、空 @」= 旧的纯播报。
    _, watch_players = _watch_targets()
    # lineup_player_ids() 内部就是 parse_lineup 再取 id,这里直接从已解析的阵容里取
    player_ids = (
        {pid for lt in lineups() for pid, _ in lt.players} if watch_players else set()
    )
    recipients = resolve_recipients(match.event_id, match.team1, match.team2, player_ids)

    # 非顶级赛事却算不出任何收件人(如靠选手所属队追进来、但该选手本场替补未上)→ 不必再追,
    # 记下避免 scan 反复把它捞回来,省抓取(顶级赛事必有火喇叭群,不会走到这)。
    if not recipients and not _top_tier(match.event_id):
        if len(_no_recipient_matches) < 2000:
            _no_recipient_matches.add(lm.match_id)
        _drop_follow(lm.match_id, reason=f"无收件人(非顶级未命中订阅){match.team1} vs {match.team2}")
        return

    # 开赛提醒:仅在「刚开赛」(已完成图数低于阈值)且未发过时推送;发现得晚(已打了图)
    # 就只标记跳过,不补发陈旧的开赛卡。
    if not store.already_pushed(lm.match_id, "start"):
        finished_ct = sum(1 for mp in match.maps if mp.finished)
        if finished_ct < cfg.cs2_sub_start_skip_if_maps_done:
            await _push_start(match, lineups(), recipients)
        else:
            store.mark_pushed(lm.match_id, "start")

    for i, mp in enumerate(match.maps):
        if not mp.finished:
            continue
        # 去重要查两种键:mapstatsid 可能比胜负标记晚出现,首推可能用了
        # "图名:比分" 回退键,mapstatsid 出现后 key() 会变 → 不查旧键会重复推送。
        alt_key = f"{mp.name}:{mp.team1_score}-{mp.team2_score}"
        if store.already_pushed(lm.match_id, mp.key()) or store.already_pushed(
            lm.match_id, alt_key
        ):
            continue
        pk = f"{lm.match_id}:{mp.key()}"
        has_rating = bool(mp.team1_players or mp.team2_players)
        if not has_rating:
            first = _pending_since.setdefault(pk, now)
            if now - first < 8 * 60:  # 评分偶尔延迟,给最多 8 分钟
                # 等评分期间不要做中盘退避
                _score_stable.discard(lm.match_id)
                continue
        if await _push_map(match, i, recipients):
            store.mark_pushed(lm.match_id, mp.key())
            _pending_since.pop(pk, None)

    if not match.series_over:
        # 之前误判 series_over 留下的计时要清掉,否则真结束时 30 分钟兜底会立即放弃
        _over_since.pop(lm.match_id, None)
    if match.series_over:
        # 关键:结束≠立刻停追。BO1/决胜图打完即 series_over,但 HLTV 评分往往延迟
        # 十几分钟;此时若停追,上面那张"等评分"的战报就永远丢了(曾真实发生)。
        # 只有所有打完的图都推送完毕才停;比赛离开 /matches 没关系,追踪按 URL 轮询。
        pending = [
            mp.name
            for mp in match.maps
            if mp.finished
            and not store.already_pushed(lm.match_id, mp.key())
            and not store.already_pushed(
                lm.match_id, f"{mp.name}:{mp.team1_score}-{mp.team2_score}"
            )
        ]
        first_over = _over_since.setdefault(lm.match_id, now)
        _score_stable.discard(lm.match_id)
        if pending:
            if now - first_over < 30 * 60:
                logger.info(
                    f"[cs2] 系列赛已结束,等待生成剩余战报:"
                    f"{match.team1} vs {match.team2} · {pending}"
                )
            elif lm.match_id not in _generation_alerted:
                _generation_alerted.add(lm.match_id)
                logger.error(
                    f"[cs2] 系列赛结束超 30 分钟仍未生成战报 {pending}:"
                    f"{match.team1} vs {match.team2}；继续低频重试"
                )
                await _alert(
                    f"⚠️ cs2_results 战报生成持续失败:{match.team1} vs {match.team2}"
                    f" · {pending}；系统将继续重试"
                )
            return
        store.mark_done(lm.match_id)  # 补报去重:全部战报已生成入队；投递由 outbox 独立完成
        store.forget_old_pushed()
        _drop_follow(
            lm.match_id,
            reason=f"系列赛结束 {match.team1} vs {match.team2}",
        )


# ————————————————————————— 个人订阅:关注集 / 收件人 / 开赛卡 —————————————————————————
def _watch_targets() -> tuple[set[str], set[str]]:
    """当前被订阅的 (team_keys, player_ids);轻量 SQL,扫描/推送前查一次。"""
    try:
        return store.distinct_target_keys()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[cs2] 读取订阅目标失败: {e}")
        return set(), set()


def _match_team_keys(team1: str, team2: str) -> set[str]:
    return {names.cf(team1), names.cf(team2)} - {""}


def _watch_team_keys() -> set[str]:
    """需要在 /matches 上额外关注的队 = 显式战队订阅 ∪ 被订阅选手的当前所属队。"""
    teams, players = _watch_targets()
    keys = set(teams)
    keys |= store.get_player_team_keys(players)  # 一次查完,别每人一个事务
    return keys - {""}


def _top_tier(event_id: str | None) -> bool:
    if not event_id:
        return False
    return (
        event_id in store.whitelist_event_ids()
        and event_id not in set(cfg.cs2_force_exclude_events)
    )


def resolve_recipients(
    event_id: str | None, team1: str, team2: str, player_ids: set[str]
) -> dict[int, set[int]]:
    """本场的 {收件群 → 要 @ 的 QQ 集}。

    - 顶级赛事:所有火喇叭群都收(与旧行为一致),命中个人订阅的群附带 @;
    - 非顶级:只有命中个人订阅的群才收(并 @)。
    无任何订阅时,顶级赛事退化为「所有火喇叭群、空 @」——即旧的纯播报行为。
    """
    firehose = target_groups()
    if not firehose:
        return {}
    subs = store.recipients_for(firehose, _match_team_keys(team1, team2), player_ids)
    top = _top_tier(event_id)
    out: dict[int, set[int]] = {}
    for g in firehose:
        if top:
            out[g] = subs.get(g, set())
        elif g in subs:
            out[g] = subs[g]
    return out


async def _push_map(
    match: hltv.MatchDetail, i: int, recipients: dict[int, set[int]]
) -> bool:
    if not recipients:
        return True
    map_key = match.maps[i].key()
    store.prepare_deliveries(
        match.match_id, map_key, list(recipients.keys()), mentions=recipients
    )
    if store.get_delivery_payload(match.match_id, map_key):
        return True

    when = f"{_now()} 北京时间"
    try:
        png = await card.render_map_card(match, i, when)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[cs2] 渲染失败: {e}")
        return False
    store.set_delivery_payload(match.match_id, map_key, png)
    logger.info(f"[cs2] 战报已进入投递队列:{match.team1} vs {match.team2} · 第 {i + 1} 图")
    return True


async def _push_start(
    match: hltv.MatchDetail, lineups: list[hltv.LineupTeam], recipients: dict[int, set[int]]
) -> None:
    """开赛提醒卡:只发给「有相关个人订阅」的群,并 @ 订阅者。火喇叭群本身不收开赛卡。"""
    start_recipients = {g: q for g, q in recipients.items() if q}
    if not start_recipients:
        store.mark_pushed(match.match_id, "start")  # 无人订阅 → 标记,避免每轮重复评估
        return
    store.prepare_deliveries(
        match.match_id, "start", list(start_recipients.keys()), mentions=start_recipients
    )
    if not store.get_delivery_payload(match.match_id, "start"):
        when = f"{_now()} 北京时间"
        try:
            png = await card.render_match_start_card(match, lineups, when, "比赛开始")
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[cs2] 开赛卡渲染失败: {e}")
            return
        store.set_delivery_payload(match.match_id, "start", png)
        logger.info(
            f"[cs2] 开赛提醒已入队:{match.team1} vs {match.team2} → 群 {sorted(start_recipients)}"
        )
    store.mark_pushed(match.match_id, "start")


# ————————————————————————— 本地名录刷新 —————————————————————————
async def refresh_roster(
    *, force: bool = False, priority: FetchPriority = "warm"
) -> tuple[int, int] | None:
    """刷新本地名录:抓一次世界排行榜(~226 队 + 全部现役阵容),整体写入。

    订阅命令只查本地名录、绝不实时打 HLTV;名录靠这里低频全量刷新
    (cs2_roster_refresh_hours,默认 72h,每次仅 1 个请求)+ 追踪比赛顺路收集保鲜。
    force=False 时窗口内直接跳过;页面缓存也算(重启不重复抓)。
    """
    now = time.time()
    max_age_s = cfg.cs2_roster_refresh_hours * 3600
    if not force:
        ts = store.get_meta("roster_refreshed_at")
        if ts and now - float(ts) < max_age_s:
            return None
    html = await fetcher.get_html(
        hltv.URL_RANKING,
        wait_selector=".ranked-team",
        max_age=0 if force else max_age_s,
        priority=priority,
    )
    if not html:
        await _fail("ranking", "排行榜抓取失败")
        return None
    _ok("ranking")
    teams = hltv.parse_ranking(html)
    if not teams:
        await _fail("ranking:parse", "排行榜解析为空")
        return None
    _ok("ranking:parse")
    n_teams, n_players = store.upsert_ranking(teams)
    store.set_meta("roster_refreshed_at", str(now))
    logger.info(f"[cs2] 名录刷新完成:{n_teams} 支战队 / {n_players} 名选手")
    return n_teams, n_players


_vrs_inflight = False


async def refresh_vrs_ranking(
    *, force: bool = False, priority: FetchPriority = "warm"
) -> int | None:
    """刷新 Valve 世界排名(VRS)总榜:一次请求拿全榜(~389 队),整体写入本地表。

    HLTV 的 VRS 页是**每天一版**的快照,所以这里的节奏是「一天一次」而不是跟着比赛轮询。
    三道闸门(max_age / min_gap / 失败冷却)都在这里,任何调用方——每日任务、启动补刷、
    命令路径的后台补刷——都过同一道门,所以再多入口也不会把请求打密。返回写入队数。
    """
    global _vrs_inflight
    if not cfg.cs2_vrs_enabled:
        return None
    # 首次刷新时 vrs_refreshed_at 还没写,几条入口(启动补刷、命令后台补刷)的闸门会同时
    # 放行 → 同一页抓两遍。进程内加个在途标记,重入直接退出(事件循环单线程,标记置位
    # 之后才有 await,不会有竞态)。
    if _vrs_inflight:
        return None
    now = time.time()
    if not force:
        ts = store.get_meta("vrs_refreshed_at")
        if ts and now - float(ts) < cfg.cs2_vrs_max_age_hours * 3600:
            return None
        last = store.get_meta("vrs_attempt_at")
        # 从没成功过(首次装/刚清库)时放开 min_gap,否则要等满一个间隔才有名次可显示;
        # 失败冷却仍在下面拦着,所以最坏也就是每小时试一次。
        if ts and last and now - float(last) < cfg.cs2_vrs_min_gap_hours * 3600:
            return None
        failed = store.get_meta("vrs_failed_at")
        if failed and now - float(failed) < cfg.cs2_vrs_fail_cooldown_min * 60:
            return None
    # 先记「尝试过」再抓:抓取挂住/进程被杀也算用掉一次配额,不会重启后连着重试。
    store.set_meta("vrs_attempt_at", str(now))
    _vrs_inflight = True
    try:
        # 页面缓存一律绕开(max_age=0):节流靠上面三道闸门,不靠缓存;走到这里就是真该拉新
        # 的了,命中一份 20 小时前的旧副本反而会把 refreshed_at 刷成现在,把陈旧名次再续 20 小时。
        html = await fetcher.get_html(
            hltv.URL_VRS_RANKING,
            wait_selector=".ranked-team",
            max_age=0,
            priority=priority,
        )
        if not html:
            store.set_meta("vrs_failed_at", str(time.time()))
            await _fail("vrs", "Valve 排名抓取失败")
            return None
        _ok("vrs")
        teams, snapshot = hltv.parse_vrs_ranking(html)
        if not teams:
            store.set_meta("vrs_failed_at", str(time.time()))
            await _fail("vrs:parse", "Valve 排名解析为空")
            return None
        _ok("vrs:parse")
        n = store.upsert_vrs_ranking(teams)
        store.set_meta("vrs_refreshed_at", str(time.time()))
        store.set_meta("vrs_snapshot_date", snapshot)
        logger.info(f"[cs2] Valve 排名刷新完成:{n} 支战队(快照 {snapshot or '未知'})")
        return n
    finally:
        _vrs_inflight = False


def note_match_vrs(match: hltv.MatchDetail) -> None:
    """从比赛页 VRS 面板顺路把两队的最新名次收进本地表(零额外请求)。

    这就是「名次随每场比赛更新」的来路:追踪中的比赛本来就在反复重抓比赛页,面板上写着
    这两支队此刻的 VRS 名次,赛果一出 HLTV 自己就改了,我们跟着写下来即可,不必去刷总榜。
    取哪一格与 render._vrs_panel 的口径一致:
    - HLTV 已结算(settled)→ 中列就是**赛后**名次;
    - 系列赛已分胜负但还没结算 → 按本场胜负从预测里取对应的一档(比留着赛前值准);
    - 还在打 → 左列的当前名次。
    只有赛前/当前那一格的 points 是绝对值(`1238pt`),其余是增减量(`+32pt`),不能当积分写。
    """
    if not (cfg.cs2_vrs_enabled and match.vrs):
        return
    pair = match.vrs.pair(match.team1, match.team2)
    if not pair:
        return
    winner = None
    if match.series_over and match.series1 != match.series2:
        winner = "team1" if match.series1 > match.series2 else "team2"
    rows = []
    for side, name, row in zip(("team1", "team2"), (match.team1, match.team2), pair, strict=True):
        if match.vrs.settled:
            cell, absolute = row.win, False
        elif winner:
            cell, absolute = (row.win if side == winner else row.lose), False
        else:
            cell, absolute = row.current, True
        cell = cell or row.current  # 该档缺失(HLTV 偶发少列)→ 退回当前值,总比不写强
        if not (name and cell and cell.rank):
            continue
        pts = re.match(r"(\d[\d,]*)", cell.points or "") if absolute else None
        rows.append((name, cell.rank, int(pts.group(1).replace(",", "")) if pts else None))
    if rows:
        try:
            store.note_vrs_from_match(rows)
        except Exception as e:  # noqa: BLE001 —— 顺路收集失败不该影响战报推送
            logger.warning(f"[cs2] VRS 顺路收集失败: {e}")


def vrs_ranks_for_card() -> dict[str, int]:
    """给卡片用的 {队名: VRS 名次};顺带在本地表过期时**后台**补刷一次(绝不阻塞命令)。"""
    if not cfg.cs2_vrs_enabled:
        return {}
    try:
        ranks = store.vrs_ranks()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[cs2] 读取 VRS 名次失败: {e}")
        return {}
    # 同名任务会被 _spawn_background 去重,和启动补刷不会撞在一起;内部自判闸门,不到期即 no-op
    _spawn_background(refresh_vrs_ranking(), name="vrs-refresh")
    return ranks


# ————————————————————————— 定时任务 —————————————————————————
async def _job_featured() -> None:
    if not target_groups():
        return
    await refresh_whitelist()
    # 预热赛事方形 logo + 今日队标:命令路径就几乎不用现场抓,首查即有图。
    # 后台跑(会被 120s 节流拖长),不占任何用户命令等待。
    _spawn_background(_prewarm_logos(), name="prewarm-daily")
    # 名录过期检查(内部按 cs2_roster_refresh_hours 自行判断,不到期为 no-op)
    _spawn_background(refresh_roster(), name="roster-daily")


async def _job_vrs() -> None:
    if target_groups():
        await refresh_vrs_ranking()


async def _poll_once() -> float:
    """公平执行一个到期操作，不把多个上游请求捆在同一轮里。

    抓取层本身会串行和节流；这里每次只提交一个 scan/backstop/follow，避免定时周期
    小于单轮耗时而不断跳过任务。直播比赛按最久未轮询优先，超过容量时也不会饿死
    排在字典后面的比赛。
    """
    global _last_backstop, _next_backstop_at, _next_scan_at
    if not target_groups():
        return 30.0

    now = time.time()
    # 有 live 时拉长 /matches 扫描:新开赛发现可慢一点,把导航额度留给逐图轮询
    scan_every = cfg.cs2_matches_scan_interval * 60
    if _followed:
        scan_every = max(scan_every, 5 * 60)
    if _next_backstop_at <= 0:
        # 启动后尽快做一次宽窗口补报
        _next_backstop_at = now + (15 if _startup_backstop_pending else 60)
    if now >= _next_scan_at:
        succeeded = await scan_live()
        _next_scan_at = time.time() + (scan_every if succeeded else min(60, scan_every))
        if succeeded:
            _stat["last_scan"] = time.time()
        _stat["last_poll"] = _now()
        return 0.0

    # 补报独立于 /matches 扫描，降低 /results 请求频率；即使有直播也保证不会永久饿死。
    backstop_every = max(cfg.cs2_cache_results_ttl, 10 * 60)
    if now >= _next_backstop_at:
        succeeded = False
        try:
            succeeded = await scan_backstop(now)
            if succeeded:
                _last_backstop = time.time()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cs2] 补报扫描失败: {e}")
        _next_backstop_at = time.time() + (backstop_every if succeeded else 60)
        _stat["last_poll"] = _now()
        return 0.0

    if _followed:
        due_matches = []
        for candidate in _followed.values():
            interval = _follow_poll_interval(candidate.match_id)
            due_at = _follow_last.get(candidate.match_id, 0.0) + interval
            due_matches.append((due_at, candidate))
        due_at, lm = min(due_matches, key=lambda item: item[0])
        if due_at <= now:
            _follow_last[lm.match_id] = now
            await follow_match(lm, now)
            if lm.match_id not in _followed:
                _follow_last.pop(lm.match_id, None)
            _stat["last_poll"] = _now()
            return 0.0

    due = [_next_scan_at, _next_backstop_at]
    due.extend(
        _follow_last.get(mid, 0.0) + _follow_poll_interval(mid) for mid in _followed
    )
    return max(1.0, min(30.0, min(due) - time.time()))


async def _poll_loop() -> None:
    """单一长期轮询器；取消时立即退出，其他异常退避后继续。"""
    while True:
        try:
            delay = await _poll_once()
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[cs2] 轮询循环异常，30 秒后重试: {e}")
            await asyncio.sleep(30)


async def _outbox_loop() -> None:
    """持续消费持久化投递队列；不依赖比赛是否仍在直播追踪中。"""
    try:
        while True:
            try:
                result = await delivery_worker.run_once(limit=50)
                if result.dead:
                    await _alert(f"⚠️ cs2_results 新增 {result.dead} 条投递死信，请检查 /cs2 状态")
                delay = 0.2 if result.claimed else cfg.cs2_delivery_poll_seconds
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.exception(f"[cs2] outbox consumer 异常，10 秒后重试: {e}")
                await asyncio.sleep(10)
    finally:
        try:
            delivery_worker.release_claims()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cs2] 释放 outbox lease 失败: {e}")


async def _job_warm_event() -> None:
    """给"正在进行"赛事的总览页(/cs2 赛程 的数据源)定期保鲜。

    /matches、/results 有 _job_poll 每几分钟保鲜,但赛事总览页(bracket 那一页)只在有人发
    /cs2 赛程 时才现抓,两次查询之间会陈旧最久 cs2_stale_event_page(1 小时)。瑞士轮换轮刚
    抽签的时刻,用户就会看到抽签前的旧对阵图(见记忆 cs2-results-plugin)。这里每隔几分钟把
    进行中赛事的总览页刷成 ≤ 一个保鲜周期新,/cs2 赛程 便总能从新缓存秒回。无进行中赛事时不抓。
    """
    if not target_groups():
        return
    try:
        ongoing = await _ongoing_event_ids(priority="warm")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[cs2] 赛事总览页保鲜:判定进行中赛事失败: {e}")
        return
    if not ongoing:
        return
    order = sorted(ongoing)
    if len(order) > cfg.cs2_event_warm_cap:
        # 多届同时进行且超过上限:今日活跃(直播/有比赛)的优先保鲜,轮空的本轮先不刷
        try:
            active = [e for e in await _active_whitelist_events(priority="warm") if e in ongoing]
        except Exception:  # noqa: BLE001
            active = []
        order = active + [e for e in order if e not in active]
    wl = store.whitelist_view()
    for eid in order[: cfg.cs2_event_warm_cap]:
        slug = (wl.get(eid) or {}).get("slug", "")
        url = f"{hltv.BASE}/events/{eid}/{slug or 'x'}"
        try:
            # max_age = 保鲜周期:刚被 /cs2 赛程 抓过(仍新)就跳过,否则同步重抓刷新缓存。
            # 不传 stale_age → 过期即真抓(后台任务里同步阻塞无妨,不占用用户命令的等待)。
            await fetcher.get_html(
                url,
                wait_selector=".event-hub-title, .swiss-visual-container, "
                ".slotted-bracket-placeholder",
                max_age=cfg.cs2_cache_event_page_ttl,
                priority="warm",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cs2] 赛事总览页保鲜失败 {eid}: {e}")


async def _job_cleanup() -> None:
    """每日缓存清理:过期页面、久未使用的 logo、失效的赛事 logo 映射、推送去重表。"""
    pages = store.prune_page_cache(max(cfg.cs2_cache_events_ttl, cfg.cs2_cache_matches_ttl, 86400))
    logos = store.prune_logos(cfg.cs2_logo_keep_days)
    evmap = store.prune_event_logos()
    deliveries = store.prune_delivery_batches(cfg.cs2_delivery_keep_days)
    store.forget_old_pushed()
    _no_recipient_matches.clear()  # 每日清空「无收件人」记忆,让替补/名单变动后能重新评估
    _roster_sighted.clear()  # 顺路收集去重表同样每日清零(重复 upsert 无害,只是省写)
    logger.info(
        f"[cs2] 缓存清理完成:页面 {pages} 个、logo {logos} 个、"
        f"赛事 logo 映射 {evmap} 条、投递批次 {deliveries} 个"
    )


@driver.on_startup
async def _startup() -> None:
    global _last_backstop, _next_backstop_at, _next_scan_at, _poll_task, fetcher
    global _startup_backstop_pending

    if fetcher.closed:
        fetcher = Fetcher(cfg)
        _next_scan_at = 0.0
        _next_backstop_at = 0.0
        _last_backstop = 0.0
        _startup_backstop_pending = True

    seeded = store.seed_subscriptions_once(cfg.cs2_subscribed_groups)
    if seeded:
        logger.info(f"[cs2] 已从配置一次性迁移 {seeded} 个订阅群到状态库")

    async def _first() -> None:
        await asyncio.sleep(10)  # 等 NapCat 连上
        if target_groups():
            try:
                await refresh_whitelist()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[cs2] 启动刷新白名单失败: {e}")
            # 重启后也预热 logo,首查即有图(不必等到每日 4 点的刷新任务)
            _spawn_background(_prewarm_logos(), name="prewarm-startup")
            # 本地名录过期就补一次(内部自判窗口;不到期为 no-op,不额外打 HLTV)
            _spawn_background(refresh_roster(), name="roster-startup")
            # VRS 总榜同理:超龄才抓,重启再频繁也不会多打请求
            _spawn_background(refresh_vrs_ranking(), name="vrs-refresh")

    _poll_task = _spawn_background(_poll_loop(), name="poll-loop")
    _spawn_background(_outbox_loop(), name="outbox-loop")
    _spawn_background(_first(), name="startup-refresh")
    _spawn_background(
        _run_interval(
            _job_warm_event,
            seconds=cfg.cs2_event_warm_interval * 60,
            initial_delay=cfg.cs2_event_warm_interval * 60,
            name="event-warm",
        ),
        name="event-warm-loop",
    )
    _spawn_background(
        _run_daily(
            _job_featured,
            hour=cfg.cs2_featured_refresh_hour,
            minute=0,
            name="featured-refresh",
        ),
        name="featured-loop",
    )
    _spawn_background(
        _run_daily(
            _job_cleanup,
            hour=cfg.cs2_cache_cleanup_hour,
            minute=20,
            name="cache-cleanup",
        ),
        name="cleanup-loop",
    )
    if cfg.cs2_vrs_enabled:
        # VRS 总榜每天一版 → 每天定时抓一次即可(内部还有 max_age/min_gap 闸门兜底)
        _spawn_background(
            _run_daily(
                _job_vrs, hour=cfg.cs2_vrs_refresh_hour, minute=30, name="vrs-refresh"
            ),
            name="vrs-loop",
        )
    logger.info(f"[cs2] 插件已加载,目标群 {sorted(target_groups())}")


@driver.on_shutdown
async def _shutdown() -> None:
    tasks = list(_background_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await fetcher.shutdown()


# ————————————————————————— 订阅维护 —————————————————————————
# 机器人被踢 / 主动退群 / 群解散时 OneBot 会推 group_decrease；此时立刻退订，
# 不必等下次投递失败再清理。投递路径对「永久不可达」也会走同一套 unsubscribe。
_group_leave = on_notice(priority=5, block=False)


@_group_leave.handle()
async def _on_group_decrease(event: GroupDecreaseNoticeEvent) -> None:
    if event.user_id == event.self_id:
        drop_unreachable_subscription(
            event.group_id,
            reason=f"机器人离群 notice sub_type={event.sub_type}",
        )
        return
    # 普通成员退群:清掉其在该群的个人订阅(退群后 @ 不到人,留着是死数据)。
    try:
        n = store.prune_user_targets(event.group_id, event.user_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[cs2] 清理退群成员订阅失败 group={event.group_id} qq={event.user_id}: {e}")
        return
    if n:
        logger.info(f"[cs2] 成员 {event.user_id} 退群 {event.group_id},清理 {n} 条订阅")


@_group_leave.handle()
async def _on_group_ban(event: GroupBanNoticeEvent) -> None:
    """跟踪「本机器人发不出言」的禁言状态,让投递侧提前避让 / 及时恢复。

    只关心两种:``user_id == 0`` 是**全员禁言**(非管理员一律发不出去),
    ``user_id == self_id`` 是机器人被单独禁言。别的成员被禁言与推送无关。
    全员禁言的 duration 为 0(无限期),交给 note_group_muted 退回默认静默期即可 ——
    真解禁时会收到 lift_ban,立刻恢复,不用等静默期自然到期。
    """
    if event.user_id not in (0, event.self_id):
        return
    scope = "全员禁言" if event.user_id == 0 else "机器人被禁言"
    if event.sub_type == "lift_ban":
        if delivery_worker.clear_group_mute(event.group_id):
            logger.info(f"[cs2] 群 {event.group_id} 已解除{scope},恢复推送")
        return
    delivery_worker.note_group_muted(event.group_id, seconds=event.duration)
    logger.info(
        f"[cs2] 群 {event.group_id} 开启{scope}"
        f"{f'({event.duration}s)' if event.duration else ''},暂停推送"
    )


# ————————————————————————— 命令 —————————————————————————
cs2 = on_command("cs2", priority=10, block=True)


def _can_manage_sub(event: MessageEvent) -> bool:
    """能否订阅/退订本群:群主、群管理员,或机器人 SUPERUSER 均可。

    群成员角色取自 GroupMessageEvent.sender.role(OneBot v11 随消息事件带上,
    值为 owner/admin/member),无需额外调用接口。"""
    if str(event.user_id) in driver.config.superusers:
        return True
    return isinstance(event, GroupMessageEvent) and event.sender.role in ("owner", "admin")


def _roster_status_line() -> str:
    ro = store.roster_overview()
    when = (
        datetime.fromtimestamp(ro["refreshed_at"], CN).strftime("%m-%d %H:%M")
        if ro["refreshed_at"]
        else "—"
    )
    return f"本地名录:{ro['teams']} 战队 / {ro['players']} 选手,上次全量刷新 {when}"


def _vrs_status_line() -> str:
    if not cfg.cs2_vrs_enabled:
        return "Valve 排名:已关闭"
    vo = store.vrs_overview()
    when = (
        datetime.fromtimestamp(vo["refreshed_at"], CN).strftime("%m-%d %H:%M")
        if vo["refreshed_at"]
        else "—"
    )
    return (
        f"Valve 排名:{vo['teams']} 队(快照 {vo['snapshot'] or '—'},上次全量刷新 {when};"
        f"其中 {vo['from_match']} 条来自比赛页实时更新)"
    )


def _in_debug_group(event: MessageEvent) -> bool:
    return isinstance(event, GroupMessageEvent) and event.group_id in set(cfg.cs2_debug_groups)


def _admin_ctx(event: MessageEvent) -> bool:
    """能用管理/调试命令(/cs2 状态、/cs2 测试)并看到「管理·调试」版帮助卡的场景:
    调试群内,或超管私聊。

    刻意排除「超管在普通群」——否则超管在普通群误发 /cs2 状态 就会把订阅信息倒进普通群,
    与「不向普通群暴露」的要求相悖。超管要调试请去调试群或私聊机器人。"""
    if _in_debug_group(event):
        return True
    is_super = str(event.user_id) in driver.config.superusers
    return is_super and not isinstance(event, GroupMessageEvent)


# ————————————————————————— 个人订阅命令 —————————————————————————
async def _handle_target_sub(event: MessageEvent, kind: str, name: str, add: bool) -> None:
    """/cs2 订阅|退订 战队|选手 <名字>。任何群成员可自助;门槛:本群已开启 /cs2 订阅。"""
    label = "战队" if kind == "team" else "选手"
    if not isinstance(event, GroupMessageEvent):
        await cs2.finish("请在群里发这个命令,订阅会绑定到「你 + 本群」,比赛时在群里 @ 你")
    gid = event.group_id
    if gid not in target_groups():
        await cs2.finish(
            "本群还没开启 CS2 推送。请群主/管理员先发 /cs2 订阅,之后群成员才能订阅具体战队/选手"
        )
    if not name:
        eg = "Vitality" if kind == "team" else "ZywOo"
        await cs2.finish(f"用法:/cs2 {'订阅' if add else '退订'} {label} <名字>,例如 /cs2 {'订阅' if add else '退订'} {label} {eg}")
    if add:
        await _target_add(event, gid, kind, label, name)
    else:
        await _target_remove(event, gid, kind, label, name)


async def _finish_add(result: str, label: str, disp: str) -> None:
    if result == "added":
        await cs2.finish(f"已订阅{label}「{disp}」✅ 之后每场比赛开赛和赛果都会在群里 @ 你")
    if result == "exists":
        await cs2.finish(f"你已经订阅过{label}「{disp}」啦")
    if result == "full":
        await cs2.finish(f"你的订阅数量已达上限({cfg.cs2_sub_max_targets_per_user}),先退订一些再来")
    await cs2.finish("订阅保存失败,请稍后再试")


async def _target_add(
    event: GroupMessageEvent, gid: int, kind: str, label: str, name: str
) -> None:
    # 本地名录解析:秒回、零 HLTV 请求。名录 = 世界排行榜低频全量刷新(refresh_roster)
    # + 追踪比赛顺路收集;订阅时绝不实时打 HLTV。
    if kind == "team":
        res = names.resolve_team_local(name, store.all_index_teams())
    else:
        res = names.resolve_player_local(name, store.all_index_players())

    if res.status == "error":
        await cs2.finish(
            "名录还没初始化(机器人刚部署或还没抓过排行榜),几分钟后再试;"
            "管理员也可在调试群发 /cs2 刷新名录"
        )
    if res.status == "none":
        sugg = ""
        if res.candidates:
            shown = [getattr(c, "nick", "") or getattr(c, "name", "") for c in res.candidates]
            sugg = ",是不是想找:" + " / ".join(s for s in shown if s)
        await cs2.finish(
            f"名录里没找到{label}「{name}」{sugg}\n"
            f"名录含 HLTV 世界排名全部战队与现役选手,每 {int(cfg.cs2_roster_refresh_hours)} 小时"
            f"自动刷新,机器人追踪的比赛也会随时补充;太冷门或刚转会的稍后再试"
        )
    if res.status == "ambiguous":
        if kind == "team":
            lines = "\n".join(f"· {t.name}" for t in res.candidates)
        else:
            lines = "\n".join(
                f"· {p.nick}" + (f"({p.team})" if p.team else "")
                for p in res.candidates
            )
        await cs2.finish(f"找到多个{label},请用更精确的写法(原样大小写/昵称原形)重发:\n{lines}")

    # 存储放 try 里;结果消息用 cs2.finish 放 try 外——FinishedException 是 Exception 子类,
    # 若在 try 内 finish 会被 except 吞掉误报「保存失败」。
    r = ""
    disp = ""
    try:
        if kind == "team":
            team = res.team
            disp = team.name
            r = store.add_target(
                gid, event.user_id, "team", names.team_key(team.name), team.name,
                max_per_user=cfg.cs2_sub_max_targets_per_user,
            )
        else:
            p = res.player
            disp = p.nick + (f"(现效力 {p.team})" if p.team else "")
            r = store.add_target(
                gid, event.user_id, "player", p.id, p.nick,
                max_per_user=cfg.cs2_sub_max_targets_per_user,
            )
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[cs2] 保存订阅失败: {e}")
        await cs2.finish("订阅保存失败,请稍后再试")
    await _finish_add(r, label, disp)


async def _target_remove(
    event: GroupMessageEvent, gid: int, kind: str, label: str, name: str
) -> None:
    # 退订不打 HLTV:只在本人已有订阅里按名字匹配(大小写 + leet 宽松)。
    mine = [t for t in store.list_targets(gid, event.user_id) if t.kind == kind]
    hit = None
    for t in mine:
        if (
            names.nick_matches(name, t.display)
            or names.cf(name) == names.cf(t.display)
            or (kind == "team" and names.team_key(name) == t.target_key)
        ):
            hit = t
            break
    if not hit:
        tail = ("。你订阅的" + label + ":" + "、".join(t.display for t in mine)) if mine else ""
        await cs2.finish(f"你没有订阅{label}「{name}」{tail}")
    try:
        ok = store.remove_target(gid, event.user_id, kind, hit.target_key)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[cs2] 退订失败: {e}")
        await cs2.finish("退订失败,请稍后再试")
    await cs2.finish(f"已退订{label}「{hit.display}」" if ok else f"你没有订阅{label}「{hit.display}」")


async def _handle_my_subs(event: MessageEvent) -> None:
    if not isinstance(event, GroupMessageEvent):
        await cs2.finish("请在群里发这个命令")
    mine = store.list_targets(event.group_id, event.user_id)
    if not mine:
        await cs2.finish("你在本群还没有订阅任何战队/选手。用 /cs2 订阅 战队 <名字> 或 /cs2 订阅 选手 <名字>")
    teams = [t.display for t in mine if t.kind == "team"]
    players = [t.display for t in mine if t.kind == "player"]
    lines = ["你在本群的订阅:"]
    if teams:
        lines.append("战队:" + "、".join(teams))
    if players:
        lines.append("选手:" + "、".join(players))
    await cs2.finish("\n".join(lines))


@cs2.handle()
async def handle_cs2(event: MessageEvent, args: Message = CommandArg()) -> None:
    raw = args.extract_plain_text().strip()
    parts = raw.split()
    sub = parts[0] if parts else ""
    admin = _admin_ctx(event)  # 调试群 / 超管私聊:才放行管理命令、才给管理版帮助

    if sub not in ("订阅", "sub", "subscribe", "退订", "unsub", "unsubscribe"):
        if left := _cooldown_left(event):
            await cs2.finish(f"请求有点密，请 {left} 秒后再试")

    if sub in ("订阅", "sub", "subscribe", "退订", "unsub", "unsubscribe"):
        add = sub in ("订阅", "sub", "subscribe")
        # 个人级:/cs2 订阅|退订 战队|选手 <名字> —— 任何群成员可自助订阅并被 @。
        if len(parts) >= 2 and parts[1] in _TEAM_WORDS | _PLAYER_WORDS:
            kind = "team" if parts[1] in _TEAM_WORDS else "player"
            name = " ".join(parts[2:]).strip()
            await _handle_target_sub(event, kind, name, add)
        # 群级:/cs2 订阅|退订 —— 群主/管理员/超管开关整个群的顶级赛事播报。
        if not isinstance(event, GroupMessageEvent):
            await cs2.finish("请在群里发这个命令")
        if not _can_manage_sub(event):
            await cs2.finish("只有群主或群管理员能" + ("订阅" if add else "退订") + "哦")
        try:
            ok = store.subscribe(event.group_id) if add else store.unsubscribe(event.group_id)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[cs2] 保存{'订阅' if add else '退订'}失败: {e}")
            await cs2.finish("订阅状态暂时无法保存，请稍后再试")
        if add:
            await cs2.finish("本群已加入 CS2 战报推送 ✅" if ok else "本群早就订阅过啦")
        await cs2.finish("本群已退出 CS2 战报推送" if ok else "本群本来就没订阅")

    if sub in ("我的订阅", "我的", "mysubs", "mine"):
        await _handle_my_subs(event)

    # —— 查询(公开)——
    if sub in ("赛事", "events"):
        await _handle_events()

    if sub in ("日程", "schedule", "sched", "今日"):
        await _handle_schedule()

    if sub in ("赛程", "bracket", "对阵", "赛程表"):
        await _handle_bracket(" ".join(parts[1:]).strip() or None)

    # —— 管理 / 调试:仅调试群或超管私聊。普通场景不匹配 → 落到帮助卡,不暴露其存在 ——
    if admin and sub in ("状态", "status"):
        wl = store.whitelist_view()
        cache = store.cache_overview()
        outbox = store.outbox_overview()
        failing = {
            name: state["fail_streak"]
            for name, state in _stat["sources"].items()
            if state["fail_streak"]
        }
        lines = [
            "📊 cs2_results 状态",
            f"订阅群:{sorted(target_groups()) or '无'}",
            f"调试群:{sorted(cfg.cs2_debug_groups) or '无'}",
            f"个人订阅:{len(store.all_targets())} 条(战队/选手)",
            _roster_status_line(),
            _vrs_status_line(),
            f"顶级赛事白名单:{len(wl)} 个",
            f"正在追踪直播:{len(_followed)} 场",
            f"缓存:logo {cache['logos']} 个 / 页面 {cache['pages']} 个,共 {cache['kb']} KB",
            f"投递:待发 {outbox['pending']} / 重试 {outbox['retry']} / "
            f"死信 {outbox['dead']} / 已取消 {outbox['cancelled']}",
            f"投递载荷:{outbox['payload_batches']} 批 / {outbox['payload_bytes'] // 1024} KB",
            f"上次刷新白名单:{_stat['last_featured'] or '—'}",
            f"上次轮询:{_stat['last_poll'] or '—'}",
            f"连续失败:{_stat['fail_streak']}",
            f"失败来源:{failing or '无'}",
            f"最近错误:{_stat['last_error'] or '—'}",
        ]
        await cs2.finish("\n".join(lines))

    if admin and sub in ("测试", "test"):
        await _handle_test(parts[1] if len(parts) > 1 else None)

    if admin and sub in ("重试投递", "retry-delivery"):
        match_id = parts[1] if len(parts) > 1 else None
        if match_id and (not match_id.isascii() or not match_id.isdecimal()):
            await cs2.finish("比赛 ID 必须是 ASCII 数字")
        replayed = store.replay_dead(match_id=match_id)
        await cs2.finish(f"已重新激活 {replayed} 条死信投递")

    if admin and sub in ("刷新名录", "refresh-roster"):
        await cs2.send("正在刷新名录(抓一次世界排行榜)…")
        try:
            result = await refresh_roster(force=True, priority="user")
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[cs2] 名录强制刷新失败: {e}")
            result = None
        if result:
            await cs2.finish(f"名录已刷新:{result[0]} 支战队 / {result[1]} 名选手")
        await cs2.finish("名录刷新失败(可能被 HLTV 限流),稍后再试")

    if admin and sub in ("刷新vrs", "刷新VRS", "refresh-vrs"):
        if not cfg.cs2_vrs_enabled:
            await cs2.finish("Valve 排名功能已关闭(CS2_VRS_ENABLED=false)")
        await cs2.send("正在刷新 Valve 世界排名(抓一次总榜)…")
        try:
            n = await refresh_vrs_ranking(force=True, priority="user")
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[cs2] Valve 排名强制刷新失败: {e}")
            n = None
        if n:
            await cs2.finish(f"Valve 排名已刷新:{n} 支战队\n{_vrs_status_line()}")
        await cs2.finish("Valve 排名刷新失败(可能被 HLTV 限流),稍后再试")

    # —— 无子命令 / 未识别 → 图片功能菜单(管理场景多显示「管理·调试」一节)——
    await _send_help(admin)


async def _send_help(admin: bool) -> None:
    try:
        png = await card.render_help_card(admin, _now())
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[cs2] 帮助卡渲染失败,降级为文字: {e}")
        lines = [
            "CS2 战报机器人 🎮",
            "/cs2 赛事 —— 未来 3 个月顶级赛事",
            "/cs2 日程 —— 当前/下个比赛日的关注赛事比赛(含赛果与直播)",
            "/cs2 赛程 [赛事名] —— 正在进行赛事的完整赛程(小组赛/淘汰赛)",
            "/cs2 订阅 / 退订 —— 本群加入/退出推送(群管理员)",
            "/cs2 订阅 战队|选手 <名字> —— 开赛提醒和每张地图赛果都 @ 你",
            "/cs2 我的订阅 —— 查看你在本群订阅的战队/选手",
        ]
        if admin:
            lines += [
                "/cs2 状态 —— 运行状态(仅调试群)",
                "/cs2 测试 [比赛ID或URL] —— 立即渲染一张战报卡(仅调试群)",
                "/cs2 重试投递 [比赛ID] —— 重新激活死信(仅调试群)",
                "/cs2 刷新名录 —— 强制刷新战队/选手名录(仅调试群)",
                "/cs2 刷新VRS —— 强制刷新 Valve 世界排名总榜(仅调试群)",
            ]
        await cs2.finish("\n".join(lines))
    await cs2.finish(MessageSegment.image(png))


async def _handle_events() -> None:
    html = await fetcher.get_html(
        hltv.URL_EVENTS,
        wait_selector="#FEATURED, .events-month",
        max_age=cfg.cs2_cache_events_ttl,
        stale_age=cfg.cs2_stale_events,
        priority="user",
    )
    if not html:
        await cs2.finish("抓 HLTV events 失败(可能被 Cloudflare 挡),稍后再试")
    now = time.time() * 1000
    window = now + 90 * 86400 * 1000
    evs = [
        e
        for e in hltv.parse_events(html)
        if e.start_unix and e.start_unix <= window and (e.end_unix or e.start_unix) >= now
    ]
    evs.sort(key=lambda e: e.start_unix)
    if not evs:
        await cs2.finish("未来 3 个月暂无顶级赛事")
    # 用缓存里的 logo 立即出图(没有的用首字母兜底),缺失的后台补齐,下次即有图。
    # 绝不再让命令卡在 120s 节流下现抓赛事页(曾导致「首次加载…」十几分钟不出图)。
    for e in evs:
        e.logo = store.get_event_logo_url(e.id)
    if any(_event_logo_missing(e) for e in evs):
        _spawn_background(
            _bg_ensure_event_logos(evs, cap=cfg.cs2_events_logo_fetch_cap),
            name="event-logo-fill",
        )
    try:
        png = await card.render_events_card(evs, _now())
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[cs2] 赛事卡渲染失败: {e}")
        await cs2.finish("渲染失败，请稍后再试")
    await cs2.finish(MessageSegment.image(png))


# 直播中读缓存比赛页的容忍窗口。大比分只在一张图打完时才变(一张图 25 分钟起),所以
# 20 分钟内的副本几乎必然仍是当前大比分——远比"只写进行中"有用。窗口不能只按
# cs2_live_poll_interval 算:抓取节流(CS2_REQUEST_MIN_GAP,本机 120s)会把单场的实际
# 刷新拉到 5-15 分钟,窄窗口下缓存里明明有 1:0 也会被判过期而丢掉。
_LIVE_SCORE_MAX_AGE = 20 * 60

# 已解析比赛页的记忆库:key = (url, 该份缓存的写入时间),value = 解析结果。
# 页面缓存副本没换过,解析结果就一定一样(parse_match 不依赖当前时间),所以这里
# 命中即可直接复用。此前 /cs2 日程、/cs2 赛程 每次都要把每场直播的 1.1MB 比赛页
# 重解析一遍(~27ms × 直播场数),而缓存副本往往几分钟都没变。
_MATCH_MEMO: OrderedDict[tuple[str, float], hltv.MatchDetail] = OrderedDict()
_MATCH_MEMO_MAX = 24


def _cached_match(url: str, mid: str = "") -> hltv.MatchDetail | None:
    """从页面缓存里取一场比赛的解析结果,零额外请求。

    实时(回合)比分静态页拿不到(scorebot websocket 注入),但直播追踪一直在抓比赛页并
    回写页面缓存 → 进行中的 BO3/BO5 能从这份副本里读出**已打完小场**的大比分。
    真过期(比赛早已结束 / 从未追踪)则返回 None,让调用方退回原状态。
    """
    if not url:
        return None
    max_age = max(cfg.cs2_live_poll_interval * 60 * 3, _LIVE_SCORE_MAX_AGE)
    hit = store.cache_get_with_ts(url, max_age)
    if not hit:
        return None
    ts, cached = hit
    memo_key = (url, ts)
    memo = _MATCH_MEMO.get(memo_key)
    if memo is not None:
        _MATCH_MEMO.move_to_end(memo_key)
        return memo
    try:
        detail = hltv.parse_match(cached, url)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[cs2] 解析缓存比赛页失败 {mid or url}: {e}")
        return None
    _MATCH_MEMO[memo_key] = detail
    _MATCH_MEMO.move_to_end(memo_key)
    while len(_MATCH_MEMO) > _MATCH_MEMO_MAX:
        _MATCH_MEMO.popitem(last=False)
    return detail


def _live_schedule_rows(html: hltv.Html, wl: set[str], excl: set[str]) -> list[hltv.ScheduledMatch]:
    """/matches 上白名单赛事的直播比赛 → 日程行(status="live"),带目前大场比分。"""
    rows = []
    for lm in hltv.parse_live_matches(html):
        if not (lm.event_id and lm.event_id in wl and lm.event_id not in excl):
            continue
        sm = hltv.ScheduledMatch(
            lm.match_id,
            lm.event_id,
            lm.event_name,
            lm.event_logo,
            lm.team1,
            lm.team2,
            0,
            "",
            lm.team1_logo,
            lm.team2_logo,
            status="live",
            url=lm.url,
        )
        d = _cached_match(lm.url, lm.match_id)
        if d:
            sm.best_of = f"bo{d.best_of}"
            if d.best_of > 1:  # bo1 无大场概念,不显示 0:0
                sm.score1, sm.score2 = d.series1, d.series2
        rows.append(sm)
    return rows


def _finished_schedule_rows(rhtml: str, start_ms: float) -> list[hltv.ScheduledMatch]:
    """/results 上今天已结束、且属于白名单赛事的比赛(按赛事 slug 后缀/赛事名匹配,
    结果行没有 data-event-id)。比分由 /results 天然给出:bo1 单场、bo3/5 大场。"""
    slugs = {s for s in store.whitelist_slugs() if s}
    names = {(v.get("name") or "").lower() for v in store.whitelist_view().values()}
    names.discard("")
    out = []
    for rm in hltv.parse_results(rhtml):
        if rm.start_unix < start_ms:
            continue
        path = rm.url.rstrip("/")
        if any(path.endswith(s) for s in slugs) or rm.event_name.lower() in names:
            out.append(rm)
    return out


def _cluster_match_days(rows: list[hltv.ScheduledMatch]) -> list[list[hltv.ScheduledMatch]]:
    """把比赛按时间空档聚成「比赛日」——CS2 的一个比赛日不等于一个日历日。

    欧洲赛事常 18:00 开打、跨午夜打到次日 01:30,日内场间隔约 2.5h、日间空档 15h+,
    所以「相邻两场开赛间隔 > gap 就切一刀」能干净地还原真实的一晚。max_span 是兜底:
    ESL 那种多线并行的赛事可能整天不断档,不设上限会把整周连成一段。

    注意:已结束行的 ``start_unix`` 是 /results 的**结束**时间(见 ScheduledMatch),
    比开赛晚 1–2.5h;远小于 gap,不影响切分。live 行没有时间戳,不参与聚类。
    """
    return hltv.cluster_match_days(
        rows,
        gap_ms=cfg.cs2_match_day_gap_hours * 3600_000,
        cap_ms=cfg.cs2_match_day_max_span_hours * 3600_000,
    )


def _match_day_span_text(day: list[hltv.ScheduledMatch]) -> str:
    """比赛日副标题:同日 "7月21日 周二 18:00–23:00";跨午夜 "… 18:00 — 次日 01:30"。"""
    a = datetime.fromtimestamp(day[0].start_unix / 1000, CN)
    b = datetime.fromtimestamp(day[-1].start_unix / 1000, CN)
    head = f"{a.month}月{a.day}日 {_WEEKDAYS_CN[a.weekday()]} {a:%H:%M}"
    if a.date() == b.date():
        return head if a == b else f"{head}–{b:%H:%M}"
    nxt = "次日" if (b.date() - a.date()).days == 1 else f"{b.month}月{b.day}日"
    return f"{head} — {nxt} {b:%H:%M}"


async def _handle_schedule() -> None:
    html = await fetcher.get_html(
        hltv.URL_MATCHES,
        wait_selector=".match",
        max_age=cfg.cs2_cache_matches_ttl,
        stale_age=cfg.cs2_stale_matches,
        priority="user",
    )
    if not html:
        await cs2.finish("抓 /matches 失败(可能被 Cloudflare 挡),稍后再试")
    wl = store.whitelist_event_ids()
    excl = set(cfg.cs2_force_exclude_events)
    now_ms = time.time() * 1000
    # 待开始取全部(不再截到今天 23:59)——比赛日由聚类切,不由日历日切。
    # 过滤未定对阵(队名为空,如未抽签的后续轮),避免"待定 vs 待定"占位。
    mtree = hltv.tree(html)  # 建一次树,待开始 + 直播两个解析器复用
    ups = [
        m
        for m in hltv.parse_upcoming_matches(mtree)
        if m.event_id and m.event_id in wl and m.event_id not in excl and m.team1 and m.team2
    ]
    lives = _live_schedule_rows(mtree, wl, excl)

    fins: list[hltv.ScheduledMatch] = []
    rhtml = await fetcher.get_html(
        hltv.URL_RESULTS,
        wait_selector=".result-con",
        max_age=cfg.cs2_cache_results_ttl,
        stale_age=cfg.cs2_stale_results,
        priority="user",
    )
    if rhtml:
        # 回看窗口要够长,才能让"上一比赛日"整段(含跨午夜那半截)进入聚类
        lookback = now_ms - (cfg.cs2_recap_max_age_hours + 24) * 3600_000
        fins = _finished_schedule_rows(rhtml, lookback)
    else:
        logger.warning("[cs2] /results 抓取失败,日程卡降级为不含已结束比赛")

    days = _cluster_match_days(fins + ups)
    if not days and not lives:
        await cs2.finish("近期没有关注赛事的比赛 🌙")

    tail = cfg.cs2_match_day_tail_hours * 3600_000
    # 「本比赛日」必须还有没打完的事:未开赛的场次,或正在直播的场次。只剩已结束的说明这一晚
    # 收工了,应当翻页到下一比赛日(旧战报走 recap 折叠附在上方)。
    # 坑:tail 是从最后一行的 start_unix 起算,而已结束行的 start_unix 其实是 /results 的
    # **结束**时间(见 _cluster_match_days),等于末场打完后还要再占 tail 小时;凌晨 4:28 打完
    # 的一晚到 7:28 都算"本比赛日",于是 5:20 查 /cs2 日程 只能看到昨晚战报,当晚 18:00 的
    # 排期被压在下一段里(用户报的 bug)。
    cur = next(
        (
            i
            for i, d in enumerate(days)
            if d[0].start_unix <= now_ms <= d[-1].start_unix + tail
            and (lives or any(m.status != "finished" for m in d))
        ),
        None,
    )
    if cur is None and lives and days:
        # 有直播 = 比赛日必然在进行中(直播行没时间戳,聚类看不到它);挂到时间上最近的一段
        cur = min(
            range(len(days)),
            key=lambda i: min(
                abs(days[i][0].start_unix - now_ms), abs(days[i][-1].start_unix - now_ms)
            ),
        )

    recap: list[hltv.ScheduledMatch] = []
    recap_label = None
    if not days:  # 只有直播、拿不到任何带时间戳的场次
        day, title = [], "本比赛日"
    elif cur is not None:
        day, title = days[cur], "本比赛日"
    else:
        nxt = next((i for i, d in enumerate(days) if d[0].start_unix > now_ms), None)
        if nxt is None:  # 只剩过去的比赛日 → 直接展示最后一段战报
            day, title = days[-1], "上个比赛日"
        else:
            day, title = days[nxt], "下个比赛日"
            # 空档期:上一比赛日的战报折叠附在上方(太旧的不附)
            if nxt > 0:
                prev = days[nxt - 1]
                fresh = now_ms - prev[-1].start_unix <= cfg.cs2_recap_max_age_hours * 3600_000
                if fresh and any(m.status == "finished" for m in prev):
                    recap = [m for m in prev if m.status == "finished"]
                    recap_label = f"上个比赛日 · {_match_day_span_text(prev)}"

    rows = day + lives
    subtitle = _match_day_span_text(day) if day else None

    # 赛事 + 两队队标(全取自列表页卡片,通常已缓存;缺的后台补,当次用首字母兜底,不阻塞出图)
    need, seen = [], set()
    for m in rows + recap:
        for u in (m.event_logo, m.team1_logo, m.team2_logo):
            if u and u not in seen and not store.has_logo(u):
                need.append(u)
                seen.add(u)
    if need:
        fetcher.spawn_logos(hltv.URL_MATCHES, need, priority="warm")
    try:
        png = await card.render_schedule_card(
            rows,
            _now(),
            title=title,
            subtitle=subtitle,
            recap=recap,
            recap_label=recap_label,
            vrs=vrs_ranks_for_card(),
        )
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[cs2] 日程卡渲染失败: {e}")
        await cs2.finish("渲染失败，请稍后再试")
    await cs2.finish(MessageSegment.image(png))


def _wl_id_for_result(rm: hltv.ScheduledMatch) -> str | None:
    """/results 行没有 event_id,靠 href 赛事 slug 后缀 / 赛事名映射回白名单 id。"""
    path = rm.url.rstrip("/")
    for eid, v in store.whitelist_view().items():
        slug = v.get("slug", "")
        if slug and path.endswith(slug):
            return eid
        if v.get("name") and rm.event_name.lower() == v["name"].lower():
            return eid
    return None


async def _ongoing_event_ids(*, priority: FetchPriority = "user") -> set[str]:
    """当前正在进行的顶级赛事 id。两个来源取并集(均与白名单取交、排除 force_exclude):

    1. HLTV /events 页 #FEATURED 区块 —— HLTV 亲自标注的进行中大赛,整届进行期间都算、
       今天轮空也算(比"今天有比赛"更贴合"正在进行")。
    2. 距首个比赛日 ≤ cs2_ongoing_lead_days 天且尚未结束的即将开赛赛事 —— #FEATURED 只在
       真正开赛后才挂上,故赛前那几天靠这条,让 /cs2 赛程 能提前查到(如开赛前 3 天的
       BLAST Bounty)。用 .big-event 卡的赛事起止时间(parse_events)判定。

    读每日缓存的 /events(几乎总是缓存命中,不额外打 HLTV)。见记忆 hltv-scraping-notes。"""
    html = await fetcher.get_html(
        hltv.URL_EVENTS,
        wait_selector="#FEATURED, .events-month",
        max_age=cfg.cs2_cache_events_ttl,
        stale_age=cfg.cs2_stale_events,
        priority=priority,
    )
    if not html:
        logger.warning("[cs2] 判定正在进行赛事时抓 /events 失败,暂视为无进行中赛事")
        return set()
    wl = store.whitelist_event_ids()
    excl = set(cfg.cs2_force_exclude_events)
    etree = hltv.tree(html)  # 建一次树,#FEATURED 与赛事列表两个解析器复用
    ids = {eid for eid in hltv.featured_event_ids(etree) if eid in wl and eid not in excl}
    # 即将开赛(距首个比赛日 ≤ N 天、且未结束)的赛事也计入
    now_ms = time.time() * 1000
    lead_ms = cfg.cs2_ongoing_lead_days * 86_400_000
    today_start_ms = (
        datetime.now(CN).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
    )
    for e in hltv.parse_events(etree):
        if e.id not in wl or e.id in excl or not e.start_unix:
            continue
        if e.start_unix <= now_ms + lead_ms and (e.end_unix or e.start_unix) >= today_start_ms:
            ids.add(e.id)
    return ids


async def _active_whitelist_events(*, priority: FetchPriority = "user") -> list[str]:
    """今天有比赛(直播/待开始/已结束)的白名单赛事 id,按今日比赛数降序。

    仅用于给"正在进行"的赛事排序(直播/今日比赛多的排前面);是否"正在进行"由
    _ongoing_event_ids()(#FEATURED)判定。复用已缓存的 /matches + /results,不额外打 HLTV。"""
    from collections import Counter

    wl = store.whitelist_event_ids()
    excl = set(cfg.cs2_force_exclude_events)
    cnt: Counter = Counter()
    mhtml = await fetcher.get_html(
        hltv.URL_MATCHES,
        wait_selector=".match",
        max_age=cfg.cs2_cache_matches_ttl,
        stale_age=cfg.cs2_stale_matches,
        priority=priority,
    )
    if mhtml:
        day_end = datetime.now(CN).replace(hour=23, minute=59, second=59, microsecond=0)
        end_ms = day_end.timestamp() * 1000
        mt = hltv.tree(mhtml)  # 建一次树,两个解析器复用
        for m in hltv.parse_upcoming_matches(mt):
            if m.event_id in wl and m.event_id not in excl and m.start_unix <= end_ms:
                cnt[m.event_id] += 1
        for lm in hltv.parse_live_matches(mt):
            if lm.event_id in wl and lm.event_id not in excl:
                cnt[lm.event_id] += 2  # 直播权重高些,优先展示
    rhtml = await fetcher.get_html(
        hltv.URL_RESULTS,
        wait_selector=".result-con",
        max_age=cfg.cs2_cache_results_ttl,
        stale_age=cfg.cs2_stale_results,
        priority=priority,
    )
    if rhtml:
        start_ms = (
            datetime.now(CN).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
        )
        for rm in _finished_schedule_rows(rhtml, start_ms):
            eid = _wl_id_for_result(rm)
            if eid and eid not in excl:
                cnt[eid] += 1
    return [eid for eid, _ in cnt.most_common()]


async def _bracket_fallback(eid: str, name: str) -> None:
    """无结构化赛程(纯循环赛联赛等)时兜底:抓该赛事的比赛子页 + 结果,按日程卡呈现。"""
    rows: list[hltv.ScheduledMatch] = []
    mhtml = await fetcher.get_html(
        f"{hltv.BASE}/events/{eid}/matches",
        wait_selector=".match",
        max_age=cfg.cs2_cache_event_page_ttl,
        stale_age=cfg.cs2_stale_event_page,
        priority="user",
    )
    if mhtml:
        # 只留双方都已确定的真实比赛;赛事 matches 子页里未定对阵是带 match-id + 未来时间的
        # 空占位(队名为空),不过滤会渲染成一堆"待定 vs 待定"。子页行 event_id 为空,故不能
        # 套 _handle_schedule 的赛事过滤,只按"两队都有"判真实。
        mt = hltv.tree(mhtml)  # 建一次树,两个解析器复用
        rows += [m for m in hltv.parse_upcoming_matches(mt) if m.team1 and m.team2]
        for lm in hltv.parse_live_matches(mt):
            if not (lm.team1 and lm.team2):
                continue
            sm = hltv.ScheduledMatch(
                lm.match_id,
                lm.event_id,
                lm.event_name,
                lm.event_logo,
                lm.team1,
                lm.team2,
                0,
                "",
                lm.team1_logo,
                lm.team2_logo,
                status="live",
                url=lm.url,
            )
            d = _cached_match(lm.url, lm.match_id)  # 进行中的 BO3/BO5:补上目前大比分
            if d:
                sm.best_of = f"bo{d.best_of}"
                if d.best_of > 1:
                    sm.score1, sm.score2 = d.series1, d.series2
            rows.append(sm)
    rhtml = await fetcher.get_html(
        f"{hltv.URL_RESULTS}?event={eid}",
        wait_selector=".result-con",
        max_age=cfg.cs2_cache_results_ttl,
        stale_age=cfg.cs2_stale_results,
        priority="user",
    )
    if rhtml:
        rows += hltv.parse_results(rhtml)
    if not rows:
        await cs2.finish(f"「{name}」暂无可展示的赛程")
    need, seen = [], set()
    for m in rows:
        for u in (m.event_logo, m.team1_logo, m.team2_logo):
            if u and u not in seen and not store.has_logo(u):
                need.append(u)
                seen.add(u)
    if need:
        fetcher.spawn_logos(hltv.URL_MATCHES, need, priority="warm")
    try:
        png = await card.render_schedule_card(
            rows, _now(), title=name, subtitle="全部比赛", vrs=vrs_ranks_for_card()
        )
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[cs2] 赛程兜底渲染失败: {e}")
        await cs2.finish("渲染失败，请稍后再试")
    await cs2.finish(MessageSegment.image(png))


def _fill_live_series_scores(sched: hltv.EventSchedule, live_urls: dict[str, str]) -> None:
    """给赛程卡里**进行中**的对阵补上目前大比分(BO3/BO5 已打完的小场)。

    赛事页的 bracket/瑞士轮 JSON 只在系列赛结束后才带 matchScore,所以直播中的对阵原本
    只能标「LIVE」。但直播追踪已经把比赛页缓存在本地(见 _cached_match),这里直接读缓存
    补分,零额外请求。比赛页 URL 优先取 /matches 直播行的(与追踪写缓存时用的键一致),
    赛事页 JSON 里的 matchPageURL 作兜底。
    """
    for mu in _iter_matchups(sched):
        if not mu.live:
            continue
        d = _cached_match(live_urls.get(mu.match_id or "") or mu.url, mu.match_id or "")
        if not d:
            continue
        if not mu.best_of:
            mu.best_of = d.best_of
        if d.best_of > 1:  # bo1 无大场概念,别渲染出 0:0
            mu.score1, mu.score2 = d.series1, d.series2


def _iter_matchups(sched: hltv.EventSchedule):
    """赛程结构里的全部对阵(瑞士轮各战绩池 + 各棵对阵树的各轮)。"""
    if sched.swiss:
        for col in sched.swiss.columns:
            for cell in col.cells:
                yield from cell.matchups
    for b in sched.brackets:
        for rnd in b.all_rounds():
            yield from rnd.matchups


async def _handle_bracket(arg: str | None) -> None:
    wl = store.whitelist_view()
    ongoing = await _ongoing_event_ids()  # 只在"正在进行(#FEATURED)"的赛事里选/匹配
    # 1) 确定目标赛事
    if arg:
        al = arg.lower().strip()
        pool = {eid: v for eid, v in wl.items() if eid in ongoing}
        # 精确匹配(id / slug / 名称)优先;否则按子串,但要求 ≥2 字符,避免单字乱撞
        matches = [
            (eid, v)
            for eid, v in pool.items()
            if al == eid
            or al == (v.get("slug") or "").lower()
            or al == (v.get("name") or "").lower()
        ]
        if not matches and len(al) >= 2:
            matches = [
                (eid, v)
                for eid, v in pool.items()
                if al in (v.get("slug") or "").lower() or al in (v.get("name") or "").lower()
            ]
        if not matches:
            if ongoing:
                names = "、".join((wl.get(e, {}).get("name") or e) for e in ongoing)
                await cs2.finish(
                    f"「{arg}」不在当前正在进行的赛事里。\n"
                    f"正在进行:{names}。想看未来赛程用 /cs2 赛事。"
                )
            await cs2.finish("当前没有正在进行的顶级赛事 🌙\n想看未来赛程可用 /cs2 赛事。")
        if len(matches) > 1:
            # 都在进行中,子串仍撞多个 → 让用户写具体些
            names = "、".join(v.get("name", "") for _, v in matches[:6])
            await cs2.finish(f"「{arg}」匹配到多个正在进行的赛事:{names}。请写得更具体。")
        eid, v = matches[0]
        slug, name = v.get("slug", ""), v.get("name", "")
    else:
        if not ongoing:
            await cs2.finish("当前没有正在进行的顶级赛事 🌙\n想看未来赛程可用 /cs2 赛事。")
        # 多届同时进行时,按今日活跃度(直播/今日比赛多)排序取最活跃的一届;
        # 今日轮空的进行中赛事排在后面但仍可选
        active = [eid for eid in await _active_whitelist_events() if eid in ongoing]
        rest = sorted(eid for eid in ongoing if eid not in active)
        eid = (active + rest)[0]
        v = wl.get(eid, {})
        slug, name = v.get("slug", ""), v.get("name", "")

    # 2) 抓赛事总览页 + 直播 id
    url = f"{hltv.BASE}/events/{eid}/{slug or 'x'}"
    html = await fetcher.get_html(
        url,
        wait_selector=".event-hub-title, .swiss-visual-container, .slotted-bracket-placeholder",
        max_age=cfg.cs2_cache_event_page_ttl,
        stale_age=cfg.cs2_stale_event_page,
        priority="user",
    )
    if not html:
        await cs2.finish("抓取赛事页失败(可能被 Cloudflare 挡),稍后再试")
    live_ids: set[str] = set()
    mhtml = await fetcher.get_html(
        hltv.URL_MATCHES,
        wait_selector=".match",
        max_age=cfg.cs2_cache_matches_ttl,
        stale_age=cfg.cs2_stale_matches,
        priority="user",
    )
    live_urls: dict[str, str] = {}
    mtree = hltv.tree(mhtml) if mhtml else None
    if mtree is not None:
        # live_match_ids() 就是 parse_live_matches() 取个 id 集合,分开调等于把
        # /matches 白解析一遍;解析一次,两样都从这份结果里取。
        lives = hltv.parse_live_matches(mtree)
        live_ids = {lm.match_id for lm in lives if lm.match_id}
        live_urls = {lm.match_id: lm.url for lm in lives if lm.url}

    # 3) 解析赛程结构(mhtml 供淘汰赛对阵回填排期时间/BO——bracket JSON 未开赛场次缺这两项)
    try:
        sched = hltv.parse_event_schedule(html, eid, live_ids, matches_html=mtree)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[cs2] 赛程解析失败: {e}")
        await cs2.finish("解析赛事页失败(HLTV 页面结构可能有变),稍后再试")
    _fill_live_series_scores(sched, live_urls)
    if not sched.name:
        sched.name = name
    if not sched.has_structure():
        await _bracket_fallback(eid, sched.name or name)

    # 4) 补齐 logo(赛事 logo + 各队队标,多数已缓存):缺的后台补,当次用首字母兜底,不阻塞出图
    need = [u for u in sched.logo_urls() if not store.has_logo(u)]
    if need:
        fetcher.spawn_logos(url, need, priority="warm")

    # 5) 渲染
    try:
        png = await card.render_event_schedule_card(sched, _now(), vrs=vrs_ranks_for_card())
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[cs2] 赛程卡渲染失败: {e}")
        await cs2.finish("渲染失败，请稍后再试")
    await cs2.finish(MessageSegment.image(png))


async def _handle_test(arg: str | None) -> None:
    # 确定要测试的比赛 URL
    url = hltv_match_url(arg) if arg else None
    if arg and not url:
        await cs2.finish("只接受 HLTV 比赛 ID，或 https://www.hltv.org/matches/... 链接")
    if not url:
        # 没给参数:找一场白名单里的直播(3 分钟内的缓存足够新)
        html = await fetcher.get_html(
            hltv.URL_MATCHES,
            wait_selector=".match",
            max_age=cfg.cs2_cache_matches_ttl,
            priority="user",
        )
        if not html:
            await cs2.finish("抓 /matches 失败(可能被 Cloudflare 挡),稍后再试")
        wl = store.whitelist_event_ids()
        live_all = hltv.parse_live_matches(html)
        live_wl = [lm for lm in live_all if lm.event_id and lm.event_id in wl]
        pick = live_wl or live_all  # 优先白名单直播,没有就拿任意直播方便测试
        if not pick:
            await cs2.finish(
                "当前 HLTV 没有正在直播的比赛。可带比赛 ID/URL 测试,例:/cs2 测试 2395449"
            )
        url = pick[0].url

    match_html = await fetcher.get_html(url, wait_selector=".mapholder", priority="user")
    if not match_html:
        await cs2.finish("抓比赛页失败,稍后再试")
    match = hltv.parse_match(match_html, url)
    note_match_vrs(match)
    await ensure_logos(match)
    finished = [i for i, m in enumerate(match.maps) if m.finished]
    if not finished:
        await cs2.finish(f"{match.team1} vs {match.team2}:还没有打完的图")
    try:
        png = await card.render_map_card(match, finished[-1], f"{_now()} 北京时间(测试)")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[cs2] 测试渲染失败: {e}")
        await cs2.finish("渲染失败，请稍后再试")
    await cs2.finish(MessageSegment.image(png))
