"""HLTV 页面解析(selectolax)。选择器均来自 2026-07 实测,HLTV 改版需重新核对。

页面地址常量 + 三类解析:
- parse_whitelist(/events)   → 顶级赛事(#FEATURED ∪ .big-event)
- parse_live_matches(/matches) → 正在直播的比赛(带 event_id)
- parse_match(/matches/{id}/…) → 单场详情:格式/进程/每图比分/每人 Rating/logo
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Optional, Union

from selectolax.parser import HTMLParser, Node

# 公开解析函数的入参:原始 HTML,**或**一棵已建好的 lexbor 树。
#
# 建树本身就占单次解析耗时的 50~65%(/matches 896KB 建树 9.1ms、比赛页 1.1MB 14.2ms),
# 而同一份 HTML 在一次请求里往往要过好几个解析器(比如 /matches 既要 parse_live_matches
# 又要 parse_upcoming_matches;比赛页既要 parse_match 又要 parse_lineup)。允许把树传进
# 来,调用方就能建一次、复用多次,省掉纯重复的建树开销。传 str 的老调用方行为不变。
Html = Union[str, HTMLParser]


def tree(html: Html) -> HTMLParser:
    """把 ``str | HTMLParser`` 归一成 HTMLParser;已是树则原样返回(不重建)。"""
    return html if isinstance(html, HTMLParser) else HTMLParser(html)

BASE = "https://www.hltv.org"
URL_EVENTS = f"{BASE}/events"
URL_MATCHES = f"{BASE}/matches"
URL_RESULTS = f"{BASE}/results"
URL_RANKING = f"{BASE}/ranking/teams"  # 会重定向到最新一期(如 /ranking/teams/2026/july/13)
# Valve 世界排名(VRS)总榜。会重定向到**当日**快照(如 /valve-ranking/teams/2026/july/23),
# 即每天一版——所以每天真抓一次就够,绝不该轮询(见 __init__.refresh_vrs_ranking 的闸门)。
URL_VRS_RANKING = f"{BASE}/valve-ranking/teams"

# ——「比赛日」切分:CS2 的一个比赛日不等于一个日历日 ——
# 欧洲赛事常 18:00 开打、跨午夜打到次日 01:30(日内场间隔约 2.5h,日间空档 15h+),
# 所以按"相邻两场开赛间隔 > GAP 就切一刀"还原真实的一晚。CAP 是兜底,防多线并行的
# 赛事整天不断档把整周连成一段。/cs2 日程 与淘汰赛按比赛日分列都用这一套定义。
MATCH_DAY_GAP_MS = 6 * 3600_000
MATCH_DAY_CAP_MS = 20 * 3600_000


def cluster_match_days(
    items: list,
    gap_ms: float = MATCH_DAY_GAP_MS,
    cap_ms: float = MATCH_DAY_CAP_MS,
    key=lambda x: x.start_unix,
) -> list[list]:
    """按开赛时间空档把比赛聚成「比赛日」。无时间戳(key 取 0)的条目会被丢弃。"""
    timed = sorted((x for x in items if key(x)), key=key)
    days: list[list] = []
    for x in timed:
        if days and key(x) - key(days[-1][-1]) <= gap_ms and key(x) - key(days[-1][0]) <= cap_ms:
            days[-1].append(x)
        else:
            days.append([x])
    return days


def _norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _txt(n: Optional[Node]) -> str:
    return _norm(n.text()) if n else ""


def _int(n: Optional[Node]) -> Optional[int]:
    t = _txt(n)
    return int(t) if t.isdigit() else None


def _event_id(href: str) -> Optional[str]:
    m = re.search(r"/events/(\d+)/", href or "")
    return m.group(1) if m else None


def _match_id(href: str) -> Optional[str]:
    m = re.search(r"/matches/(\d+)/", href or "")
    return m.group(1) if m else None


# ————————————————————————— 数据结构 —————————————————————————
@dataclass
class EventRef:
    id: str
    slug: str
    name: str


@dataclass
class EventDetail:
    id: str
    name: str
    start_unix: int  # ms;用于 3 个月过滤
    end_unix: int
    date_text: str  # HLTV 原文,如 "Jul 21st - Jul 26th"
    teams: str  # 参赛队伍数,如 "32"
    prize: str
    location: str
    slug: str = ""  # 赛事页 slug(取方形 eventlogo 用)
    logo: Optional[str] = None  # 方形 eventlogo URL;懒发现后回填


@dataclass
class LiveMatch:
    match_id: str
    url: str
    event_id: Optional[str]
    event_name: str
    team1: str = ""
    team2: str = ""
    team1_logo: Optional[str] = None
    team2_logo: Optional[str] = None
    event_logo: Optional[str] = None


@dataclass
class ScheduledMatch:
    """日程卡的一行:待开始 / 进行中 / 已结束 三种状态共用。"""

    match_id: str
    event_id: Optional[str]
    event_name: str
    event_logo: Optional[str]
    team1: str
    team2: str
    start_unix: int  # ms;finished 时为 /results 的结束时间戳,live 拿不到则 0
    best_of: str  # "bo1"/"bo3"/...(来自 .match-meta;/results 无此信息则空)
    team1_logo: Optional[str] = None
    team2_logo: Optional[str] = None
    status: str = "upcoming"  # upcoming / live / finished
    score1: Optional[int] = None  # finished:赛果(bo1=单场比分,bo3/5=大场比分);
    score2: Optional[int] = None  # live:目前大场比分(bo3/5,来自缓存的比赛页)
    winner: Optional[str] = None  # "team1"/"team2",仅 finished
    url: str = ""  # /matches/{id}/{slug};slug 结尾含赛事 slug,供白名单匹配


@dataclass
class PlayerStat:
    nick: str
    rating: float
    kills: Optional[int] = None
    deaths: Optional[int] = None

    @property
    def kd(self) -> Optional[str]:
        """击杀-死亡,如 "43-16";数据缺失时为 None。"""
        if self.kills is None or self.deaths is None:
            return None
        return f"{self.kills}-{self.deaths}"


@dataclass
class MapResult:
    name: str
    mapstatsid: Optional[str]
    team1_score: Optional[int]
    team2_score: Optional[int]
    winner: Optional[str]  # "team1" / "team2" / None
    halves: str  # 如 "(5:7; 4:6)"
    finished: bool
    team1_players: list[PlayerStat] = field(default_factory=list)
    team2_players: list[PlayerStat] = field(default_factory=list)
    picked_by: Optional[str] = None  # "team1"/"team2"=选图方;"decider"=决胜图;None=BO1/未知

    def key(self) -> str:
        """去重键:优先 mapstatsid,退回图名。"""
        return self.mapstatsid or f"{self.name}:{self.team1_score}-{self.team2_score}"


@dataclass
class VrsCell:
    """VRS 面板里的一格:积分(现值或增减)+ 世界排名 + 名次变化。"""

    points: str = ""  # "1238pt" / "+32pt" / "-30pt"(原样保留 HLTV 的写法)
    rank: Optional[int] = None  # 49
    rank_delta: Optional[int] = None  # +5 / -2;None = 名次不变
    trend: str = "unchanged"  # rising / falling / unchanged(HLTV 给的涨跌类名)

    @property
    def signed(self) -> bool:
        """是否为「增减量」格(带正负号),用于上色;current 列不带号。"""
        return self.points.startswith(("+", "-"))


@dataclass
class VrsRow:
    """VRS 面板里的一支队(一行)。

    赛前(forecast)三列:current=当前、win=若获胜、lose=若失利;
    赛后(HLTV 切成 "VRS result")只剩两列:current=赛前、win=本场实际影响、lose=None。
    """

    name: str
    current: Optional[VrsCell] = None
    win: Optional[VrsCell] = None
    lose: Optional[VrsCell] = None


@dataclass
class VrsForecast:
    """比赛页的 VRS(Valve 排名)预测/赛果面板。settled=True 即 HLTV 已给出赛后实际值。"""

    settled: bool
    rows: list[VrsRow] = field(default_factory=list)  # 页面顺序 = team1、team2

    def pair(self, team1: str, team2: str) -> Optional[tuple[VrsRow, VrsRow]]:
        """对齐到 (team1 行, team2 行);名字对不上就按页面顺序兜底。"""
        if len(self.rows) < 2:
            return None
        a, b = self.rows[0], self.rows[1]
        n1, n2 = _norm(team1).lower(), _norm(team2).lower()
        if _norm(a.name).lower() == n2 and _norm(b.name).lower() == n1:
            return b, a
        return a, b


@dataclass
class MatchDetail:
    match_id: str
    url: str
    event_name: str
    event_id: Optional[str]
    stage: str
    best_of: int
    is_lan: bool
    team1: str
    team2: str
    team1_logo: Optional[str]
    team2_logo: Optional[str]
    event_logo: Optional[str]
    maps: list[MapResult]
    vrs: Optional[VrsForecast] = None

    @property
    def series1(self) -> int:
        return sum(1 for m in self.maps if m.finished and m.winner == "team1")

    @property
    def series2(self) -> int:
        return sum(1 for m in self.maps if m.finished and m.winner == "team2")

    @property
    def series_over(self) -> bool:
        need = self.best_of // 2 + 1
        return max(self.series1, self.series2) >= need or (
            self.best_of == 1 and any(m.finished for m in self.maps)
        )

    def logo_urls(self) -> list[str]:
        return [u for u in (self.team1_logo, self.team2_logo, self.event_logo) if u]


# ————————————————————————— 解析 —————————————————————————
def parse_whitelist(events_html: Html) -> list[EventRef]:
    """顶级赛事 = #FEATURED 里的(进行中)+ .big-event(即将开始的大赛)。"""
    t = tree(events_html)
    out: dict[str, EventRef] = {}

    def add(a: Node):
        href = a.attributes.get("href", "") or ""
        eid = _event_id(href)
        if not eid:
            return
        slug = href.rstrip("/").split("/")[-1]
        # 干净赛事名:big-event 用 .big-event-name;featured 用 logo img 的 title;
        # 都没有才退回 a.text()(含日期/奖金等杂讯,放最后)
        name_el = (
            a.css_first(".big-event-name")
            or a.css_first(".event-name")
            or a.css_first(".eventname")
        )
        if name_el:
            name = name_el.text()
        else:
            img = a.css_first("img[title]")
            name = (img.attributes.get("title") if img else None) or _txt(a)
        out.setdefault(eid, EventRef(eid, slug, _norm(name)))

    feat = t.css_first("#FEATURED")
    if feat:
        for a in feat.css("a[href*='/events/']"):
            add(a)
    for a in t.css("a.big-event[href*='/events/']"):
        add(a)
    return list(out.values())


def featured_event_ids(events_html: Html) -> set[str]:
    """/events 页 #FEATURED 区块里的赛事 id —— HLTV 亲自标注的"正在进行的顶级赛事"。

    #FEATURED 只放当前进行中的大赛(见记忆 hltv-scraping-notes),故用它作为
    "正在进行"的判据比"今天有比赛"更贴合语义(轮空日也仍算进行中)。无 #FEATURED
    (无进行中赛事)时返回空集。
    """
    t = tree(events_html)
    feat = t.css_first("#FEATURED")
    if not feat:
        return set()
    out: set[str] = set()
    for a in feat.css("a[href*='/events/']"):
        eid = _event_id(a.attributes.get("href", "") or "")
        if eid:
            out.add(eid)
    return out


def parse_events(events_html: Html) -> list[EventDetail]:
    """解析 events 页的 .big-event 卡片(顶级赛事),含日期/参赛队伍数,供 /cs2 赛事 展示。"""
    t = tree(events_html)
    out: list[EventDetail] = []
    seen: set[str] = set()
    for a in t.css("a.big-event[href*='/events/']"):
        href = a.attributes.get("href", "") or ""
        eid = _event_id(href)
        if not eid or eid in seen:
            continue
        seen.add(eid)
        slug = href.rstrip("/").split("/")[-1]
        name = _txt(a.css_first(".big-event-name"))
        location = _txt(a.css_first(".big-event-location"))
        start = end = 0
        date_text = teams = prize = ""
        info = a.css_first(".additional-info")
        if info:
            date_cell = info.css_first(".col-value.col-date")
            if date_cell:
                date_text = _txt(date_cell)
                spans = date_cell.css("span[data-unix]")
                if spans:
                    su = spans[0].attributes.get("data-unix", "") or ""
                    eu = spans[-1].attributes.get("data-unix", "") or ""
                    start = int(su) if su.isdigit() else 0
                    end = int(eu) if eu.isdigit() else start
            rows = info.css("tr")
            vals = rows[0].css("td.col-value") if rows else []
            if len(vals) >= 3:  # [日期, 奖金, 队伍数]
                prize = _txt(vals[1])
                teams = _txt(vals[2])
        out.append(EventDetail(eid, name, start, end, date_text, teams, prize, location, slug))
    return out


def parse_live_matches(matches_html: Html) -> list[LiveMatch]:
    """/matches 上**正在直播**的比赛。

    坑:HLTV 不在静态 HTML 里给 live 标记(比分靠 scorebot websocket 注入),
    而 `.matchLive` 其实是**星级评分**控件(match-rating),不是直播标记,千万别用。
    可靠判据:upcoming 比赛的 `.match-time` 带**未来**的 `data-unix`;正在打的比赛
    这个时间被直播比分替换 → 没有 `.match-time` 或其 `data-unix` 不在未来。已结束的
    比赛会从 /matches 移到 /results,所以"在 /matches 上且非 upcoming"即 live。
    """
    t = tree(matches_html)
    now_ms = time.time() * 1000
    by_id: dict[str, LiveMatch] = {}
    for c in t.css(".match"):
        a = c.css_first("a[href*='/matches/']")
        href = (a.attributes.get("href", "") or "") if a else ""
        mid = _match_id(href)
        if not mid:
            continue
        mt = c.css_first(".match-time")
        unix = mt.attributes.get("data-unix") if mt else None
        if unix and unix.isdigit() and int(unix) > now_ms:  # 有未来开赛时间 = 还没开打
            continue
        ev = c.css_first("[data-event-id]")
        eid = ev.attributes.get("data-event-id") if ev else None
        # 同一场在 /matches 上常有多张 .match 卡(顶部精选卡缺 data-event-id);去重时
        # **优先保留带 event_id 的那张**,否则白名单匹配会漏掉它 → 漏追直播。
        prev = by_id.get(mid)
        if prev is not None and (prev.event_id or not eid):
            continue
        evh = c.css_first("[data-event-headline]")
        ename = (evh.attributes.get("data-event-headline") if evh else "") or ""
        if not ename and ev is not None:
            ename = ev.text()
        names = [_txt(x) for x in c.css(".match-teamname")]
        team1 = names[0] if len(names) > 0 else ""
        team2 = names[1] if len(names) > 1 else ""
        limg = c.css_first(".match-event-logo")
        elogo = (
            (limg.attributes.get("data-cookieblock-src") or limg.attributes.get("src"))
            if limg
            else None
        ) or None
        by_id[mid] = LiveMatch(
            mid,
            BASE + href,
            eid,
            _norm(ename),
            team1,
            team2,
            _pick_card_team_logo(c, team1),
            _pick_card_team_logo(c, team2),
            elogo,
        )
    return list(by_id.values())


def parse_upcoming_matches(matches_html: Html) -> list[ScheduledMatch]:
    """/matches 上**待开始**(未来)的比赛,带排期时间/BOx/赛事 logo。

    与 parse_live_matches 相反:这里只要 `.match-time[data-unix]` 是**未来**时间的
    (即还没开打);进行中/已结束的跳过。赛事 logo 直接取卡片里的 `.match-event-logo`
    (`data-cookieblock-src`),省去再抓赛事页。
    """
    t = tree(matches_html)
    now_ms = time.time() * 1000
    by_id: dict[str, ScheduledMatch] = {}
    for c in t.css(".match"):
        a = c.css_first("a[href*='/matches/']")
        href = (a.attributes.get("href", "") or "") if a else ""
        mid = _match_id(href)
        if not mid:
            continue
        mt = c.css_first(".match-time")
        unix = mt.attributes.get("data-unix") if mt else None
        if not (unix and unix.isdigit()):
            continue
        start = int(unix)
        if start <= now_ms:  # 只要待开始
            continue
        evd = c.css_first("[data-event-id]")
        eid = evd.attributes.get("data-event-id") if evd else None
        # 同一场在 /matches 上常有多张 .match 卡:顶部"精选"卡**缺 data-event-id**,
        # 赛事分组里的那张才带。去重时**优先保留带 event_id 的**,否则白名单过滤会把它
        # 当成无赛事的比赛漏掉(曾导致同一赛事只显示部分场次)。
        prev = by_id.get(mid)
        if prev is not None and (prev.event_id or not eid):
            continue
        evh = c.css_first("[data-event-headline]")
        ename = (evh.attributes.get("data-event-headline") if evh else "") or ""
        if not ename and evd is not None:
            ename = evd.text()
        names = [_txt(x) for x in c.css(".match-teamname")]
        team1 = names[0] if len(names) > 0 else ""
        team2 = names[1] if len(names) > 1 else ""
        meta = _txt(c.css_first(".match-meta"))
        limg = c.css_first(".match-event-logo")
        elogo = (
            (limg.attributes.get("data-cookieblock-src") or limg.attributes.get("src"))
            if limg
            else None
        ) or None
        by_id[mid] = ScheduledMatch(
            mid,
            eid,
            _norm(ename),
            elogo,
            team1,
            team2,
            start,
            meta,
            _pick_card_team_logo(c, team1),
            _pick_card_team_logo(c, team2),
        )
    out = list(by_id.values())
    out.sort(key=lambda m: m.start_unix)
    return out


def _logo_variant_rank(cls: str) -> int:
    """浅底卡上队标变体的优先级(越小越好)。

    HLTV 给同一队标发 day-only / night-only 两版:day-only 是**为浅色背景**做的版本
    —— 深色队标它就是普通深色版,纯白队标它则是**反相后的深色版**(URL 带 invert=true);
    night-only 是深色主题版(纯白队标在这里是白的,贴浅底看不见)。我们的卡是米色浅底,
    故 day-only 最优、无 day/night 类的通用版次之、night-only 最次。这样白队标自动取到
    深色版、深色队标行为不变,无需逐张判断底色明暗(队标混用 webp/svg,无法统一测亮度)。"""
    if "day-only" in cls:
        return 0
    if "night-only" in cls:
        return 2
    return 1


def _pick_card_team_logo(
    card: Node, team: str, img_sel: str = "img.match-team-logo"
) -> Optional[str]:
    """从一张卡片/行里挑该队的队标(title 精确匹配 + URL 必须是 teamlogo)。

    在浅底卡上优先取 day-only 变体(见 _logo_variant_rank)。/results 行传 img_sel="img"。
    """
    tl = (team or "").lower()
    if not tl:
        return None
    best, best_rank = None, 99
    for img in card.css(img_sel):
        title = (img.attributes.get("title") or img.attributes.get("alt") or "").lower()
        if title != tl:
            continue
        u = img.attributes.get("data-cookieblock-src") or img.attributes.get("src") or ""
        if not u or "teamlogo/" not in u:
            continue
        rank = _logo_variant_rank(img.attributes.get("class") or "")
        if rank < best_rank:
            best, best_rank = u, rank
            if rank == 0:
                break
    return best


def parse_results(results_html: Html) -> list[ScheduledMatch]:
    """/results 上**已结束**的比赛(status="finished")。

    行选择器(2026-07 实测):`.result-con`,结束时间戳 `data-zonedgrouping-entry-unix`(ms)
    在该 div 上;顶部 "Featured results" 块**没有**时间戳且与正式列表重复 → 直接跳过。
    比分 `.result-score` 按 team1-team2 顺序,BO1 天然是单场比分、BO3/5 是大场比分;
    胜者 `.team-won`。行内没有 data-event-id,白名单匹配靠 href 的赛事 slug 后缀/赛事名。
    """
    t = tree(results_html)
    by_id: dict[str, ScheduledMatch] = {}
    for c in t.css(".result-con"):
        unix = c.attributes.get("data-zonedgrouping-entry-unix")
        if not (unix and unix.lstrip("-").isdigit()):
            continue  # Featured 块,正式列表里会再出现
        a = c.css_first("a[href*='/matches/']")
        href = (a.attributes.get("href", "") or "") if a else ""
        mid = _match_id(href)
        if not mid or mid in by_id:
            continue
        teams = [_txt(x) for x in c.css(".team")]
        if len(teams) < 2:
            continue
        m = re.match(r"(\d+)\s*-\s*(\d+)", _txt(c.css_first(".result-score")))
        s1, s2 = (int(m.group(1)), int(m.group(2))) if m else (None, None)
        won = _txt(c.css_first(".team-won"))
        winner = "team1" if won == teams[0] else ("team2" if won == teams[1] else None)
        elogo = None
        for img in c.css("img"):
            u = img.attributes.get("data-cookieblock-src") or img.attributes.get("src") or ""
            if "eventlogo/" in u:
                elogo = u
                break
        by_id[mid] = ScheduledMatch(
            mid,
            None,
            _txt(c.css_first(".event-name")),
            elogo,
            teams[0],
            teams[1],
            int(unix),
            "",
            _pick_card_team_logo(c, teams[0], "img"),
            _pick_card_team_logo(c, teams[1], "img"),
            status="finished",
            score1=s1,
            score2=s2,
            winner=winner,
            url=BASE + href,
        )
    out = list(by_id.values())
    out.sort(key=lambda r: r.start_unix)
    return out


def _pick_logo(t: HTMLParser, kind: str, title: str, allow_fallback: bool = False) -> Optional[str]:
    """在所有 img 里挑 kind(teamlogo/eventlogo)且 title 匹配的 URL,优先 day-only 变体。

    浅底卡取 day-only(见 _logo_variant_rank),纯白队标据此自动取到深色反相版。
    allow_fallback 只对唯一实体(赛事 logo)开;战队 logo 必须 title 精确匹配,
    否则宁可返回 None(回退首字母),绝不借用别的战队 logo。
    """
    title_l = (title or "").lower()
    exact, exact_rank = None, 99
    fallback, fb_rank = None, 99
    for img in t.css("img"):
        u = img.attributes.get("data-cookieblock-src") or img.attributes.get("src") or ""
        if f"{kind}/" not in u:
            continue
        rank = _logo_variant_rank(img.attributes.get("class") or "")
        it = (img.attributes.get("title") or img.attributes.get("alt") or "").lower()
        if title_l and it == title_l:
            if rank < exact_rank:
                exact, exact_rank = u, rank
        elif rank < fb_rank:
            fallback, fb_rank = u, rank
    if exact is not None:
        return exact
    return fallback if allow_fallback else None


def _parse_veto_picks(t: HTMLParser, team1: str, team2: str) -> dict[str, str]:
    """从 veto 序列解析每张图的归属 → {map_name_lower: "team1"/"team2"/"decider"}。

    veto 步骤在**第二个** `.veto-box` 里(第一个是 "Best of X" 那行),是一串
    `N. <队名> picked <Map>` / `N. <队名> removed <Map>` 的 <div>;末尾的 decider 为
    `N. <Map> was left over`(无人主动选,记为 "decider")。BO1 无 veto 序列 → 返回空。
    队名对回 team1/team2:大小写无关,精确匹配优先,再退子串包含(容忍队名细微差异)。
    """

    def side(name: str) -> Optional[str]:
        n = _norm(name).lower()
        t1, t2 = team1.lower(), team2.lower()
        if n == t1:
            return "team1"
        if n == t2:
            return "team2"
        if n and (n in t1 or t1 in n):
            return "team1"
        if n and (n in t2 or t2 in n):
            return "team2"
        return None

    picks: dict[str, str] = {}
    for vb in t.css(".veto-box"):
        txt = _txt(vb)
        if "picked" not in txt:  # 跳过 "Best of X" 那个框
            continue
        for seg in re.split(r"\d+\.\s*", txt):  # 按 "1. " "2. " … 拆成单步
            seg = seg.strip()
            m = re.match(r"(.+?)\s+picked\s+([A-Za-z0-9]+)", seg)
            if m:
                s = side(m.group(1))
                if s:
                    picks[m.group(2).lower()] = s
                continue
            d = re.match(r"([A-Za-z0-9]+)\s+was left over", seg)  # 决胜图(无人选)
            if d:
                picks[d.group(1).lower()] = "decider"
    return picks


def _vrs_cell(w: Optional[Node]) -> Optional[VrsCell]:
    """一个 .vrs-forecast-numbers-wrapper → VrsCell;空/无值返回 None。"""
    if w is None:
        return None
    points = _txt(w.css_first(".vrs-forecast-points"))
    rk = w.css_first(".vrs-forecast-ranking")
    rank, trend = None, "unchanged"
    if rk:
        m = re.search(r"#?(\d+)", _txt(rk))
        rank = int(m.group(1)) if m else None
        cls = rk.attributes.get("class", "") or ""
        trend = "rising" if "rising" in cls else "falling" if "falling" in cls else "unchanged"
    dm = re.search(r"[+-]?\d+", _txt(w.css_first(".vrs-forecast-small-points")))
    delta = int(dm.group(0)) if dm else None
    if not points and rank is None:
        return None
    return VrsCell(points, rank, delta, trend)


def parse_vrs(t: HTMLParser) -> Optional[VrsForecast]:
    """比赛页的 VRS 面板(实测 2026-07-23)。无面板(低级别赛事/未上榜)返回 None。

    赛前:标题 "VRS forecast",`.vrs-forecast-container` 内三列 —— 左 `.vrs-forecast-left-numbers`
    (表头 current)、中 `.vrs-forecast-middle`(If team wins)、右 `.vrs-forecast-right`
    (If team loses);每列两个 `.vrs-forecast-numbers-wrapper`,顺序 = 上下两支队。
    赛后:HLTV 自己把面板换成 "VRS result" —— 左列表头变 before、中列表头变 result
    (=该队本场实际增减)、**右列整个消失**;故「右列缺席」即赛后面板。
    """
    box = t.css_first(".vrs-forecast-container")
    if not box:
        return None
    names = [_txt(n) for n in box.css(".vrs-forecast-team-name")][:2]
    if len(names) < 2:
        return None
    cols: dict[str, list[Node]] = {}
    for key, sel in (
        ("current", ".vrs-forecast-left-numbers"),
        ("win", ".vrs-forecast-middle"),
        ("lose", ".vrs-forecast-right"),
    ):
        col = box.css_first(sel)
        cols[key] = col.css(".vrs-forecast-numbers-wrapper")[:2] if col else []
    head = _txt(box.css_first(".vrs-forecast-header")).lower()  # 文档序第一个 = 左列表头
    settled = not cols["lose"] or head.startswith("before")

    def cell(key: str, i: int) -> Optional[VrsCell]:
        col = cols[key]
        return _vrs_cell(col[i]) if i < len(col) else None

    rows = [
        VrsRow(nm, cell("current", i), cell("win", i), cell("lose", i))
        for i, nm in enumerate(names)
    ]
    if not any(r.current or r.win or r.lose for r in rows):
        return None
    return VrsForecast(settled, rows)


def _map_players(t: HTMLParser, mapstatsid: str) -> tuple[list[PlayerStat], list[PlayerStat]]:
    node = t.css_first(f'[id="{mapstatsid}-content"]')
    if not node:
        return [], []
    tables = node.css("table.totalstats")
    teams: list[list[PlayerStat]] = [[], []]
    for idx, tbl in enumerate(tables[:2]):
        for row in tbl.css("tr"):
            if "header-row" in (row.attributes.get("class", "") or ""):
                continue
            nick_el = row.css_first(".player-nick") or row.css_first("td.players a")
            rat_el = row.css_first("td.rating")
            if not nick_el or not rat_el:
                continue
            try:
                rating = float(_txt(rat_el))
            except ValueError:
                continue
            # K-D:取传统数据列 td.kd.traditional-data(如 "43-16");
            # 另有 td.kd.eco-adjusted-data.hidden(经济补正版,不用)。
            kills = deaths = None
            kd_el = row.css_first("td.kd.traditional-data") or row.css_first("td.kd")
            if kd_el:
                mkd = re.match(r"\s*(\d+)\s*-\s*(\d+)", _txt(kd_el))
                if mkd:
                    kills, deaths = int(mkd.group(1)), int(mkd.group(2))
            teams[idx].append(PlayerStat(_txt(nick_el), rating, kills, deaths))
    return teams[0], teams[1]


def parse_match(match_html: Html, url: str) -> MatchDetail:
    t = tree(match_html)

    # 格式 + 阶段 + LAN
    best_of, stage, is_lan = 1, "", False
    vb = t.css_first(".veto-box .preformatted-text")
    if vb:
        vt = _txt(vb)
        m = re.search(r"Best of (\d+)", vt)
        if m:
            best_of = int(m.group(1))
        is_lan = "(LAN)" in vt
        if "*" in vt:
            stage = vt.split("*", 1)[1].strip()

    # 队名
    def team_name(sel: str) -> str:
        g = t.css_first(sel)
        return _txt(g.css_first(".teamName")) if g else ""

    team1 = team_name(".team1-gradient") or "Team 1"
    team2 = team_name(".team2-gradient") or "Team 2"

    # 赛事
    ev = t.css_first(".timeAndEvent .event a") or t.css_first(".event a[href*='/events/']")
    event_name = _txt(ev) if ev else ""
    event_id = _event_id(ev.attributes.get("href", "") or "") if ev else None

    # 每图选边(BO3/5 的 veto pick;BO1 无)
    picks = _parse_veto_picks(t, team1, team2)

    # 地图
    maps: list[MapResult] = []
    for mh in t.css(".mapholder"):
        name = _txt(mh.css_first(".mapname"))
        if not name or name.upper() in ("TBA", "DEFAULT"):
            continue
        res = mh.css_first(".results")
        played = bool(res and "played" in (res.attributes.get("class", "") or ""))
        left = mh.css_first(".results-left")
        right = mh.css_first(".results-right")
        ls = _int(mh.css_first(".results-left .results-team-score"))
        rs = _int(mh.css_first(".results-right .results-team-score"))
        winner = None
        if left and "won" in (left.attributes.get("class", "") or ""):
            winner = "team1"
        elif right and "won" in (right.attributes.get("class", "") or ""):
            winner = "team2"
        halves = _txt(mh.css_first(".results-center-half-score"))
        stats_a = mh.css_first("a[href*='mapstatsid/']")
        msid = None
        if stats_a:
            m = re.search(r"mapstatsid/(\d+)/", stats_a.attributes.get("href", "") or "")
            msid = m.group(1) if m else None
        # 坑(2026-07-02 两轮实测):直播中的图,.results 就带 played 类、scorebot 的
        # 实时比分在 .results-team-score 里(6-6 半场被当终局);甚至 won/lost 类在直播
        # 中标的是"当前领先/落后方"(5-7 半场 TYLOO 就有 won)——都不是入库信号。
        # 唯一可靠的"结果已提交"标志是 mapstatsid(STATS 统计页链接,入库才生成;
        # 历史上所有正确推送的去重键都是它,评分也挂在它下面)。
        finished = (
            played and ls is not None and rs is not None and winner is not None and msid is not None
        )
        t1p, t2p = _map_players(t, msid) if (finished and msid) else ([], [])
        maps.append(
            MapResult(
                name, msid, ls, rs, winner, halves, finished, t1p, t2p, picks.get(name.lower())
            )
        )

    return MatchDetail(
        match_id=_match_id(url) or "",
        url=url,
        event_name=event_name,
        event_id=event_id,
        stage=stage,
        best_of=best_of,
        is_lan=is_lan,
        team1=team1,
        team2=team2,
        team1_logo=_pick_logo(t, "teamlogo", team1),
        team2_logo=_pick_logo(t, "teamlogo", team2),
        event_logo=_pick_logo(t, "eventlogo", event_name, allow_fallback=True),
        maps=maps,
        vrs=parse_vrs(t),
    )


# ═══════════════════════════ 赛程(整届赛事结构:小组赛/瑞士轮 + 淘汰赛) ═══════════════════════════
# 数据取自赛事总览页 /events/{id}/{slug}(全部服务端渲染):
#   · 瑞士轮 .swiss-visual-container —— 每场 .swiss-visual-matchup[data-match-details-popup-json]
#   · 淘汰赛 .slotted-bracket-placeholder[data-slotted-bracket-json] —— 整棵对阵树 JSON
#   · 循环赛 .groups-container .group —— 积分表(顶级赛事已很少用,兜底)
# 瑞士轮的每场 popup JSON 与淘汰赛 bracket 的每个 slot 用**同一套 matchup DTO**,故共用 _matchup()。
# 详见记忆 hltv-event-schedule-dom。


@dataclass
class SlotTeam:
    """对阵里的一支占位:已确定用真实战队(name+logo),未定则给文字描述(如 "Winner of …")。"""

    name: str = ""
    logo: Optional[str] = None
    ranking: Optional[int] = None
    desc: str = ""  # 未定队伍的文字占位(HLTV 的 TextDescription/OneOf)

    @property
    def known(self) -> bool:
        return bool(self.name)


@dataclass
class Matchup:
    """一场对阵(瑞士轮 / 淘汰赛通用)。未开打时 score 为空、队伍可能是占位。"""

    match_id: Optional[str]
    url: str
    start_unix: int  # ms;未排期为 0
    best_of: int
    team1: SlotTeam
    team2: SlotTeam
    score1: Optional[int] = None
    score2: Optional[int] = None
    winner: Optional[str] = None  # "team1"/"team2"/None
    finished: bool = False
    live: bool = False

    def logo_urls(self) -> list[str]:
        return [u for u in (self.team1.logo, self.team2.logo) if u]


@dataclass
class SwissCell:
    """瑞士轮一个战绩块:普通=该战绩下的对阵;晋级/淘汰=一组战队(只有队标)。"""

    record: str  # "2:0" 等
    kind: str  # "normal" / "advanced" / "eliminated"
    matchups: list[Matchup] = field(default_factory=list)
    teams: list[SlotTeam] = field(default_factory=list)


@dataclass
class SwissColumn:
    status: str  # finished / active / upcoming
    cells: list[SwissCell] = field(default_factory=list)


@dataclass
class SwissStage:
    columns: list[SwissColumn] = field(default_factory=list)

    def logo_urls(self) -> list[str]:
        out = []
        for col in self.columns:
            for cell in col.cells:
                for mu in cell.matchups:
                    out += mu.logo_urls()
                out += [t.logo for t in cell.teams if t.logo]
        return out


@dataclass
class BracketRound:
    name: str  # 展示名(已尽量中文化)
    matchups: list[Matchup] = field(default_factory=list)
    stage: str = ""  # 归一化英文阶段名(quarterfinal/semifinal/grandfinal…),供 /matches 排期回填对应
    requires_opponent_selection: bool = False

    def is_pending(self) -> bool:
        """本轮是否尚未产生可展示的对阵。

        普通赛事沿用 HLTV 的场次状态：已完赛、直播、已建场或已排期均算已产生。BLAST
        Bounty 由上一轮胜者挑对手，HLTV 的固定树和预留时段都不能代表真实对阵；该赛制
        必须等双方确定且正式建场后才展示。
        """
        if not self.requires_opponent_selection:
            return not any(
                mu.finished or mu.live or mu.match_id or mu.start_unix for mu in self.matchups
            )
        return not any(
            mu.finished
            or mu.live
            or (
                mu.team1.known
                and mu.team2.known
                and bool(mu.match_id or mu.start_unix)
            )
            for mu in self.matchups
        )


@dataclass
class Bracket:
    kind: str  # "single" / "double"
    name: str
    upper: list[BracketRound] = field(default_factory=list)  # single:全部轮次;double:胜者组
    lower: list[BracketRound] = field(default_factory=list)  # 仅 double:败者组
    finals: list[BracketRound] = field(default_factory=list)  # double:决赛阶段

    def all_rounds(self) -> list[BracketRound]:
        return self.upper + self.lower + self.finals

    def is_pending(self) -> bool:
        """整棵树都还没确定队伍(赛事尚未进淘汰赛)→ 只需展示赛制概要,不必铺满空对阵。"""
        return all(r.is_pending() for r in self.all_rounds())

    def known_rounds(self) -> list[BracketRound]:
        """已产生对阵的轮次(渲染成对阵树)。"""
        return [r for r in self.all_rounds() if not r.is_pending()]

    def pending_rounds(self) -> list[BracketRound]:
        """尚未产生对阵的轮次(渲染成一行赛制摘要)。"""
        return [r for r in self.all_rounds() if r.is_pending()]

    def logo_urls(self) -> list[str]:
        out = []
        for r in self.all_rounds():
            for mu in r.matchups:
                out += mu.logo_urls()
        return out


@dataclass
class GroupStanding:
    name: str
    header: list[str]
    rows: list[dict] = field(default_factory=list)  # {rank,name,logo,cells:[str]}

    def logo_urls(self) -> list[str]:
        return [r["logo"] for r in self.rows if r.get("logo")]


@dataclass
class EventSchedule:
    event_id: str
    name: str
    logo: Optional[str]
    date_text: str
    prize: str
    location: str
    status: str  # "Live" / "Upcoming" / …
    swiss: Optional[SwissStage] = None
    # 一个赛事页可能挂多棵对阵树(HLTV 的 "Stage 1"/"Stage 2"),按"有内容优先"排序
    brackets: list[Bracket] = field(default_factory=list)
    groups: list[GroupStanding] = field(default_factory=list)

    def has_structure(self) -> bool:
        return bool(self.swiss or self.brackets or self.groups)

    def logo_urls(self) -> list[str]:
        out = [self.logo] if self.logo else []
        if self.swiss:
            out += self.swiss.logo_urls()
        for b in self.brackets:
            out += b.logo_urls()
        for g in self.groups:
            out += g.logo_urls()
        return [u for u in dict.fromkeys(out) if u]  # 去重保序


# ————————————————— matchup DTO(瑞士轮 popup / 淘汰赛 slot 共用)—————————————————
def _slot_team(ts: Optional[dict]) -> SlotTeam:
    ts = ts if isinstance(ts, dict) else {}
    ty = (ts.get("type") or "").split(".")[-1]
    if ty == "FixedTeam":
        tm = ts.get("team")
        tm = tm if isinstance(tm, dict) else {}
        tl = tm.get("teamLogo")
        tl = tl if isinstance(tl, dict) else {}
        logo = tl.get("dayLogoURL") or tl.get("nightLogoURL")  # dayLogo=浅底版,正合米色卡
        return SlotTeam(_norm(tm.get("name", "")), logo, tm.get("ranking"))
    if ty == "OneOf":
        # 决赛槽的两名候选(上一轮未打完):HLTV 展示两候选,取其名做占位("A / B")
        cands = ts.get("candidates")
        names = [_norm((c or {}).get("name", "")) for c in cands] if isinstance(cands, list) else []
        names = [n for n in names if n]
        if names:
            return SlotTeam(desc=" / ".join(names))
    return SlotTeam(desc=_norm(ts.get("description") or ""))  # TextDescription/未定 → 占位


def _matchup(mu: Optional[dict], live_ids: set[str]) -> Matchup:
    mu = mu if isinstance(mu, dict) else {}
    match = mu.get("match")
    match = match if isinstance(match, dict) else {}
    result = mu.get("result")
    result = result if isinstance(result, dict) else None
    t1 = _slot_team(mu.get("team1"))
    t2 = _slot_team(mu.get("team2"))
    mid = match.get("matchId")
    mid = str(int(mid)) if isinstance(mid, (int, float)) else (str(mid) if mid else None)
    purl = match.get("matchPageURL") or ""
    url = (BASE + purl) if purl.startswith("/") else purl
    try:
        start = int(match.get("startTime") or 0)
    except (TypeError, ValueError):
        start = 0
    # 没有 match 对象 = 该场次还没排期(BLAST 这类"每轮结束才挑对手"的赛制里,后续轮
    # 长期如此)→ bo 留 0 表示未知,别默认成 1,否则空轮次会渲染出假的 "BO1"。
    try:
        bo = int(match.get("numberOfMaps") or 0)
    except (TypeError, ValueError):
        bo = 0
    s1 = s2 = None
    winner = None
    finished = False
    live = bool(mid and mid in live_ids)
    ms = result.get("matchScore") if result else None
    ms = ms if isinstance(ms, dict) else None
    if ms:
        s1, s2 = ms.get("team1Score"), ms.get("team2Score")
        winner = "team1" if ms.get("team1Winner") else "team2"
        finished = not live  # 结果已入库即完赛;若同时在直播列表里则以直播为准
    return Matchup(mid, url, start, bo, t1, t2, s1, s2, winner, finished, live)


# ————————————————————————— 瑞士轮 —————————————————————————
def _swiss_cluster_team(node: Node) -> SlotTeam:
    """晋级/淘汰块里的一支战队(无 popup JSON,只有队标 img)。取 day-only 变体。"""
    best, best_rank, title = None, 99, ""
    for img in node.css("img"):
        u = img.attributes.get("data-cookieblock-src") or img.attributes.get("src") or ""
        if not u or "teamlogo/" not in u:
            continue
        rank = _logo_variant_rank(img.attributes.get("class") or "")
        if rank < best_rank:
            best, best_rank = u, rank
            title = img.attributes.get("title") or img.attributes.get("alt") or ""
    return SlotTeam(_norm(title), best)


def parse_swiss(t: HTMLParser, live_ids: set[str]) -> Optional[SwissStage]:
    cont = t.css_first(".swiss-visual-container")
    if not cont:
        return None
    stage = SwissStage()
    for col in cont.css(".swiss-visual-column"):
        classes = (col.attributes.get("class") or "").split()
        status = next((c for c in ("finished", "active", "upcoming") if c in classes), "")
        column = SwissColumn(status=status)
        for w in col.css(".swiss-visual-matchups-wrapper"):
            wc = w.attributes.get("class") or ""
            kind = (
                "advanced" if "advanced" in wc else "eliminated" if "eliminated" in wc else "normal"
            )
            titles = [_txt(x) for x in w.css(".swiss-visual-matchups-title") if _txt(x)]
            record = " / ".join(dict.fromkeys(titles))
            cell = SwissCell(record=record, kind=kind)
            pool_slots: list[SlotTeam] = []  # 未抽签分组池的成员(不配对)
            for m in w.css(".swiss-visual-matchup"):
                raw = m.attributes.get("data-match-details-popup-json")
                if raw:
                    try:
                        cell.matchups.append(_matchup(json.loads(raw), live_ids))
                    except (ValueError, KeyError):
                        continue
                else:
                    # 未定场次(本轮未抽签/上一轮未打完):配对尚不确定,HLTV 只是把队伍
                    # 两两排版,并非真实对阵。故只收集队伍当「分组池成员」,不当成对阵。
                    pool_slots += [_swiss_cluster_team(s) for s in m.css(".swiss-visual-team")[:2]]
            for tw in w.css(".swiss-matchups-team-wrapper .swiss-visual-team"):
                st = _swiss_cluster_team(tw)
                if st.logo or st.name:
                    cell.teams.append(st)
            # 普通池若还没抽出对阵、但已有池成员 → 存成分组池成员(渲染成不配对的队伍簇)
            if kind == "normal" and not cell.matchups and pool_slots:
                cell.teams = pool_slots
            if cell.matchups or cell.teams:
                column.cells.append(cell)
        if column.cells:
            stage.columns.append(column)
    return stage if stage.columns else None


def _fill_cluster_logos(swiss: Optional[SwissStage], bracket: Optional[Bracket]) -> None:
    """把晋级/淘汰带、分组池里的队标换成对阵里的 dayLogoURL(浅底深色版)。

    坑:晋级/淘汰带与未抽签分组池的 `.swiss-visual-team` img 只有**单一**变体
    `swiss-visual-team-logo`(非 day/night),白队标(BIG / EYEBALLERS / 9z 等)取到的
    就是白色版,贴米色卡几乎看不见(见 bug:已晋级/已淘汰队伍浅色配色难读)。但这些队都
    在本页 popup-JSON 对阵里出现过,`_slot_team` 已从那里取到 dayLogoURL(白队标的反相深色
    版)。故按队名把簇内队标覆盖成对阵里的 dayLogoURL,白队标即取到可见的深色版。"""
    if not swiss:
        return
    day: dict[str, str] = {}

    def _collect(mu: Matchup) -> None:
        for tm in (mu.team1, mu.team2):
            if tm.name and tm.logo:
                day.setdefault(tm.name.lower(), tm.logo)

    for col in swiss.columns:
        for cell in col.cells:
            for mu in cell.matchups:
                _collect(mu)
    if bracket:
        for r in bracket.all_rounds():
            for mu in r.matchups:
                _collect(mu)
    if not day:
        return
    for col in swiss.columns:
        for cell in col.cells:
            for tm in cell.teams:
                better = day.get(tm.name.lower()) if tm.name else None
                if better:
                    tm.logo = better


# ————————————————————————— 淘汰赛(bracket JSON)—————————————————————————
_CN_ROUND = {
    "grand final": "总决赛",
    "grandfinal": "总决赛",
    "final": "决赛",
    "semi-finals": "半决赛",
    "semifinals": "半决赛",
    "semi finals": "半决赛",
    "quarter-finals": "八强赛",
    "quarterfinals": "八强赛",
    "quarter finals": "八强赛",
    "round of 16": "十六强",
    "round of 32": "三十二强",
    "round of 8": "八强赛",
    "opening round": "首轮",
    "consolidation final": "季军赛",
    "lower final": "败者组决赛",
    "lower semis": "败者组半决赛",
    "upper final": "胜者组决赛",
    "upper semifinals": "胜者组半决赛",
    "upper quarterfinals": "胜者组八强",
    "elimination round": "淘汰轮",
}


def _pretty_round(raw: str) -> str:
    """把 slotId 前缀 / roundName 转成展示名(尽量中文)。

    坑:剥尾部数字是为了处理 slotId 的场次后缀("Quarterfinals1"→"Quarterfinals"),
    但 HLTV 的 roundName 本身就带数字("Round of 16"),先剥再查表会剥成 "Round of"
    → 查不到 → 显示成 "Round Of"(用户实际看到的)。故**原样先查一次表**。
    """
    if (raw or "").strip().lower() in _CN_ROUND:
        return _CN_ROUND[raw.strip().lower()]
    s = re.sub(r"\d+$", "", raw or "").strip()
    s = re.sub(r"Match$", "", s).strip()
    # 驼峰拆词:GrandFinal → Grand Final;RoundOf16 → Round Of 16
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    s = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", s)
    key = s.lower().replace("  ", " ").strip()
    if key in _CN_ROUND:
        return _CN_ROUND[key]
    # 常见词单独兜底
    for k, v in _CN_ROUND.items():
        if key == k:
            return v
    return s.title() if s else "对阵"


def _norm_stage(s: str) -> str:
    """阶段名归一,让淘汰赛轮次与 /matches 场次能对上:去非字母、去 class 前缀 match、去尾复数。
    'Quarter-finals' / 'Quarterfinals1' / 'match-quarterfinal ' → 'quarterfinal';'Grand final' → 'grandfinal'。"""
    s = re.sub(r"[^a-z]", "", (s or "").lower())
    if s.startswith("match"):
        s = s[len("match") :]
    return s[:-1] if s.endswith("s") else s


def _bo_int(s: str) -> int:
    """ ".match-meta" 文本 'bo3' → 3;无数字 → 0。"""
    m = re.search(r"\d+", s or "")
    return int(m.group()) if m else 0


def _round_from_slots(slots: list[dict], name_hint: str, live_ids: set[str]) -> BracketRound:
    matchups = []
    for s in slots or []:
        if not isinstance(s, dict):
            continue
        mu = s.get("matchup")
        if mu is None:
            continue
        matchups.append(_matchup(mu, live_ids))
    ids = [(s.get("slotId") or {}).get("id", "") for s in (slots or []) if isinstance(s, dict)]
    raw = name_hint or (ids[0] if ids else "")  # roundName 优先,退而用 slotId(如 "Quarterfinals1")
    label = _pretty_round(raw) if raw else "对阵"
    return BracketRound(name=label, matchups=matchups, stage=_norm_stage(raw))


# double-elim 命名轮次:展示顺序 + 分组(u 胜者组 / l 败者组 / f 决赛阶段)+ 中文名。
# 用**规范 key** 定名比 roundName 稳(lowerRound1/2/3 的 roundName 都叫 "Lower round" 会撞名)。
_DE_ORDER = [
    ("upperRound1", "u", "胜者组首轮"),
    ("upperQuarterfinals", "u", "胜者组八强"),
    ("upperSemifinals", "u", "胜者组半决赛"),
    ("upperFinal", "u", "胜者组决赛"),
    ("lowerRound1", "l", "败者组第一轮"),
    ("lowerRound2", "l", "败者组第二轮"),
    ("lowerRound3", "l", "败者组第三轮"),
    ("lowerSemis", "l", "败者组半决赛"),
    ("lowerFinal", "l", "败者组决赛"),
    ("consolidationFinal", "f", "季军赛"),
    ("grandFinal", "f", "总决赛"),
]


def parse_brackets(t: HTMLParser, live_ids: set[str]) -> list[Bracket]:
    """解析页面上**全部**淘汰赛对阵树,按页面顺序返回。

    坑(2026-07-21 实测 BLAST Bounty S2 / id 9154):一个赛事页可以挂**多个**
    ``.slotted-bracket-placeholder``,而且**先出现的可能是空的**——该赛事页第一个是
    ``Stage 2``(``display:"Collapsed"``,16 个 slot 全是 match=null / 无队伍),第二个才是
    ``Stage 1``(``display:"Expanded"``,Round of 32 的 16 场全部就位)。原来用
    ``css_first`` 只取第一个,于是 /cs2 赛程 恒渲染成"对阵将在小组赛结束后产生"的空壳,
    尽管赛程其实已排定。故必须全取。
    """
    out: list[Bracket] = []
    for ph in t.css(".slotted-bracket-placeholder"):
        raw = ph.attributes.get("data-slotted-bracket-json")
        if not raw:
            continue
        try:
            j = json.loads(raw)
        except ValueError:
            continue
        b = _bracket_from_json(j, live_ids)
        if b:
            out.append(b)
    return out


def _bracket_from_json(j: dict, live_ids: set[str]) -> Optional[Bracket]:
    kind_type = (j.get("type") or "").split(".")[-1]
    name = _norm(j.get("name") or "")

    if "SingleElimination" in kind_type:
        upper = []
        for rnd in j.get("rounds") or []:
            rn = rnd.get("roundName") if isinstance(rnd.get("roundName"), dict) else {}
            br = _round_from_slots(rnd.get("slots") or [], rn.get("name") or "", live_ids)
            if br.matchups:
                upper.append(br)
        return Bracket("single", name or "Playoffs", upper=upper) if upper else None

    # DoubleElimination(16/8/…):命名轮次是 dict,按规范 key 顺序取名;未知键兜底追加
    up, lo, fi = [], [], []
    used = set()
    for key, grp, cn in _DE_ORDER:
        rnd = j.get(key)
        if not isinstance(rnd, dict):
            continue
        used.add(key)
        br = _round_from_slots(rnd.get("slots") or [], "", live_ids)
        br.name = cn
        if not br.matchups:
            continue
        (up if grp == "u" else lo if grp == "l" else fi).append(br)
    for key, val in j.items():
        if key in used or not isinstance(val, dict) or "slots" not in val:
            continue
        rn = val.get("roundName")
        rn = rn if isinstance(rn, dict) else {}
        hint = rn.get("name") or key
        br = _round_from_slots(val.get("slots") or [], hint, live_ids)
        if not br.matchups:
            continue
        grp = "u" if key.startswith("upper") else "l" if key.startswith("lower") else "f"
        (up if grp == "u" else lo if grp == "l" else fi).append(br)
    if up or lo or fi:
        return Bracket("double", name or "Double Elimination", upper=up, lower=lo, finals=fi)
    return None


# ————————————————————————— 循环赛积分表(兜底)—————————————————————————
def parse_groups(t: HTMLParser) -> list[GroupStanding]:
    cont = t.css_first(".groups-container")
    if not cont:
        return []
    out: list[GroupStanding] = []
    for g in cont.css(".group"):
        if "swiss-mode" in (g.attributes.get("class") or ""):
            continue  # 瑞士轮已单独渲染,跳过
        tbl = g.css_first("table")
        if not tbl:
            continue
        trs = tbl.css("tr")
        if len(trs) < 2:
            continue
        header = [_txt(c) for c in trs[0].css("td, th")]
        name = header[0] if header else _txt(g.css_first(".group-name"))
        rows = []
        for tr in trs[1:]:
            cells = tr.css("td")
            if not cells:
                continue
            tname = _txt(tr.css_first(".team-name, .teamName, .team")) or _txt(cells[0])
            logo = None
            for img in tr.css("img"):
                u = img.attributes.get("data-cookieblock-src") or img.attributes.get("src") or ""
                if "teamlogo/" in u:
                    logo = u
                    break
            vals = [_txt(c) for c in cells[1:]]
            if tname:
                rows.append({"rank": len(rows) + 1, "name": tname, "logo": logo, "cells": vals})
        if rows:
            out.append(GroupStanding(name=name or "Group", header=header, rows=rows))
    return out


# ————————————————————————— 赛事总览页元信息 + 组装 —————————————————————————
def _event_meta(t: HTMLParser) -> dict:
    name = _txt(t.css_first(".event-hub-title"))
    # .eventdate 有两个节点:标签 "Date" + 值 "Jul 1st - Jul 12th 2026",取带数字的那个
    date_text = ""
    for e in t.css(".eventdate"):
        tx = _txt(e)
        if re.search(r"\d", tx) and tx.lower() != "date":
            date_text = tx
            break
    # 干净奖金:优先 .prizepool.text-ellipsis(总额 "$1,000,000");headline 那个含 "Prize pool (?)"
    # 的拆分说明要跳过
    prize = _txt(t.css_first(".prizepool.text-ellipsis"))
    if not prize or "prize pool" in prize.lower():
        for e in t.css(".prizepool"):
            tx = _txt(e)
            if ("$" in tx or tx.replace(",", "").isdigit()) and "prize pool" not in tx.lower():
                prize = tx
                break
    location = _txt(t.css_first(".location .text-ellipsis")) or _txt(t.css_first(".event-location"))
    status = _txt(t.css_first(".event-hub-indicator"))
    logo = _pick_logo(t, "eventlogo", name, allow_fallback=True)
    return {
        "name": name,
        "date_text": date_text,
        "prize": prize,
        "location": location,
        "status": status,
        "logo": logo,
    }


@dataclass(frozen=True)
class _SchedEntry:
    """/matches 上某赛事一场比赛的排期要点,供淘汰赛对阵回填时间/BO。"""

    start: int  # 开赛毫秒时间戳;缺失为 0
    bo: int  # BOx(bo3→3);缺失为 0
    stage: str  # 归一化阶段(quarterfinal/semifinal/grandfinal…)
    teams: frozenset  # 两队名(小写归一);待定场为空集


def _event_match_index(matches_html: Html, event_id: str) -> list[_SchedEntry]:
    """从 /matches 抽取某赛事全部场次的(时间, BO, 阶段, 两队)。淘汰赛对阵在 bracket JSON 里
    未开赛时 match=null(既无时间也无 BO),而 /matches 已列出这些场次并带 `.match-stage`
    (Quarterfinal/Semifinal/Grand Final)、`.match-meta`(boX)、`.match-time[data-unix]`。"""
    t = tree(matches_html)
    by_id: dict[str, _SchedEntry] = {}
    for c in t.css(".match"):
        evd = c.css_first("[data-event-id]")
        if not evd or evd.attributes.get("data-event-id") != event_id:
            continue  # 顶部精选卡无 data-event-id,自然被过滤
        a = c.css_first("a[href*='/matches/']")
        mid = _match_id(a.attributes.get("href", "") or "") if a else None
        if not mid or mid in by_id:
            continue
        mt = c.css_first(".match-time")
        unix = mt.attributes.get("data-unix") if mt else None
        start = int(unix) if (unix and unix.isdigit()) else 0
        bo = _bo_int(_txt(c.css_first(".match-meta")))
        stage = _norm_stage(_txt(c.css_first(".match-stage")))
        names = [n for n in (_norm(_txt(x)).lower() for x in c.css(".match-teamname")) if n]
        by_id[mid] = _SchedEntry(start=start, bo=bo, stage=stage, teams=frozenset(names))
    return list(by_id.values())


def _enrich_bracket_schedule(brackets: list[Bracket], entries: list[_SchedEntry]) -> None:
    """用 /matches 的排期回填淘汰赛对阵的时间与 BO。

    必须一次处理全部对阵树:赛事页可能同时挂多棵树(如 EWC 的主单败树 + 各小组
    双败树),且多棵树常有同名轮次(grandFinal 等)。若逐树按阶段名独立回填,主赛段
    总决赛的排期会被误配到小组树的同名空轮上。故每个 /matches 场次全局只认领一次,
    优先按双方队名精确匹配;序位兜底要求场次与该树参赛队伍不冲突、且归属无跨树歧义。
    BLAST Bounty 轮次已有双方名字时必须精确匹配,避免固定树的候选对手被空场次壳误确认。
    """
    if not brackets or not entries:
        return
    by_pair = {e.teams: e for e in entries if len(e.teams) == 2}
    by_stage: dict[str, list[_SchedEntry]] = {}
    for e in entries:
        by_stage.setdefault(e.stage, []).append(e)
    for grp in by_stage.values():
        grp.sort(key=lambda e: (e.start == 0, e.start))  # 已排期在前,按开赛时间序

    def _apply(mu: Matchup, e: _SchedEntry) -> None:
        if e.start and not mu.start_unix:
            mu.start_unix = e.start
        if e.bo:
            mu.best_of = e.bo  # /matches 的 BO 权威且同轮一致

    # 第一遍:跨所有树按双方队名精确认领,认领即把场次移出序位池;顺带收集每棵树的
    # 参赛队集合(判定某场次可否归属某树)与未认领对阵(留给序位兜底)。
    tree_teams: dict[int, set[str]] = {}
    pending: list[tuple[Bracket, BracketRound, Matchup]] = []
    for b in brackets:
        names = tree_teams.setdefault(id(b), set())
        for rnd in b.all_rounds():
            for mu in rnd.matchups:
                names.update(tm.name.lower() for tm in (mu.team1, mu.team2) if tm.known)
                has_team1 = mu.team1.known
                has_team2 = mu.team2.known
                if has_team1 and has_team2:
                    e = by_pair.get(frozenset({mu.team1.name.lower(), mu.team2.name.lower()}))
                    if e is not None:
                        _apply(mu, e)
                        pool = by_stage.get(e.stage)
                        if pool and e in pool:
                            pool.remove(e)
                        continue
                    if rnd.requires_opponent_selection:
                        continue
                elif rnd.requires_opponent_selection and (has_team1 or has_team2):
                    continue
                pending.append((b, rnd, mu))

    # 第二遍:阶段序位兜底。同一阶段的待回填对阵若分布在多棵树,场次必须能唯一归属
    # 到当前树(队名都属于本树且不与他树撞)才可落位;双方全待定的场次只在无歧义时取。
    stage_trees: dict[str, set[int]] = {}
    for b, rnd, _mu in pending:
        stage_trees.setdefault(rnd.stage, set()).add(id(b))
    for b, rnd, mu in pending:
        pool = by_stage.get(rnd.stage)
        if not pool:
            continue
        unique_tree = stage_trees[rnd.stage] == {id(b)}
        known = {tm.name.lower() for tm in (mu.team1, mu.team2) if tm.known}
        e = None
        for cand in pool:
            if known and cand.teams:
                ok = known <= cand.teams  # 对阵已知的队名必须都出现在场次里
            elif cand.teams:
                homes = [t for t in stage_trees[rnd.stage] if cand.teams <= tree_teams[t]]
                ok = homes == [id(b)] or unique_tree
            else:
                ok = unique_tree  # 双方全待定的场次,跨树同名轮次无法归属
            if ok:
                e = cand
                break
        if e is None:
            continue
        pool.remove(e)
        _apply(mu, e)


def _requires_opponent_selection(event_name: str) -> bool:
    """仅 BLAST Bounty 使用每轮完赛后由胜者挑选下一轮对手的机制。"""
    return "blast bounty" in (event_name or "").casefold()


def parse_event_schedule(
    overview_html: Html,
    event_id: str,
    live_ids: Optional[set[str]] = None,
    matches_html: Optional[Html] = None,
) -> EventSchedule:
    """解析赛事总览页 → 完整赛程(瑞士轮 / 淘汰赛 / 循环赛)。live_ids 供标记直播场;
    matches_html(/matches 页)供淘汰赛对阵回填排期时间与 BO(bracket JSON 未开赛场次缺这两项)。"""
    live_ids = live_ids or set()
    t = tree(overview_html)
    meta = _event_meta(t)
    swiss = parse_swiss(t, live_ids)
    brackets = parse_brackets(t, live_ids)
    requires_opponent_selection = _requires_opponent_selection(meta["name"])
    for b in brackets:
        for rnd in b.all_rounds():
            rnd.requires_opponent_selection = requires_opponent_selection
    # 有内容的阶段排前面:HLTV 的页面顺序不保证(9154 就是空的 Stage 2 在前)
    brackets.sort(key=lambda b: (b.is_pending(), 0))
    for b in brackets:
        _fill_cluster_logos(swiss, b)  # 晋级/淘汰带白队标 → 对阵里的深色版,避免浅底难读
    if brackets and matches_html:
        _enrich_bracket_schedule(brackets, _event_match_index(matches_html, event_id))
    groups = [] if swiss else parse_groups(t)
    return EventSchedule(
        event_id=event_id,
        name=meta["name"],
        logo=meta["logo"],
        date_text=meta["date_text"],
        prize=meta["prize"],
        location=meta["location"],
        status=meta["status"],
        swiss=swiss,
        brackets=brackets,
        groups=groups,
    )


def live_match_ids(matches_html: Html) -> set[str]:
    """从 /matches 提取正在直播的比赛 id(给赛程里标 LIVE 用)。"""
    return {lm.match_id for lm in parse_live_matches(matches_html) if lm.match_id}


# ═══════════════════════════ 阵容 + 搜索(战队/选手订阅用)═══════════════════════════
@dataclass
class LineupTeam:
    name: str
    ordinal: int  # 1 或 2,对应 team1-gradient / team2-gradient
    players: list[tuple[str, str]] = field(default_factory=list)  # [(player_id, nick), ...]
    rank: Optional[int] = None  # HLTV 世界排名(阵容框标题里的 "World rank:#N")
    logo: Optional[str] = None


def parse_lineup(match_html: Html) -> list[LineupTeam]:
    """从比赛页解析双方首发五人。开赛前数小时即可用。

    player id + team 归属取自 `.player-compare[data-player-id][data-team-ordinal]`,
    昵称取自照片 img 的 alt(格式 "Denis 'deko' Zhukov",引号内即昵称)。
    """
    t = tree(match_html)
    buckets: dict[int, list[tuple[str, str]]] = {1: [], 2: []}
    seen: set[tuple[int, str]] = set()
    for pc in t.css(".player-compare[data-player-id]"):
        pid = (pc.attributes.get("data-player-id") or "").strip()
        ordi_raw = (pc.attributes.get("data-team-ordinal") or "").strip()
        if not pid or ordi_raw not in ("1", "2"):
            continue
        ordi = int(ordi_raw)
        if (ordi, pid) in seen:
            continue
        seen.add((ordi, pid))
        img = pc.css_first("img")
        nick = ""
        if img:
            alt = img.attributes.get("alt") or img.attributes.get("title") or ""
            m = re.search(r"'([^']+)'", alt)
            nick = m.group(1) if m else ""
        if not nick:
            nk = pc.css_first(".player-nick")
            nick = _txt(nk)
        buckets[ordi].append((pid, nick))
    names = {1: "", 2: ""}
    ranks: dict[int, Optional[int]] = {1: None, 2: None}
    logos: dict[int, Optional[str]] = {1: None, 2: None}
    for i, box in enumerate(t.css(".lineup.standard-box")[:2], start=1):
        a = box.css_first("a[href*='/team/']")
        names[i] = _txt(a) if a else ""
        hd = box.css_first(".box-headline")
        if hd:
            rm = re.search(r"#(\d+)", _txt(hd))
            if rm:
                ranks[i] = int(rm.group(1))
        limg = box.css_first("img.logo") or box.css_first("img[title]")
        if limg:
            logos[i] = limg.attributes.get("data-cookieblock-src") or limg.attributes.get("src")
    out: list[LineupTeam] = []
    for ordi in (1, 2):
        if buckets[ordi]:
            out.append(
                LineupTeam(names.get(ordi, ""), ordi, buckets[ordi][:5], ranks[ordi], logos[ordi])
            )
    return out


def lineup_player_ids(match_html: Html) -> set[str]:
    """比赛页首发全部 player id(供选手订阅命中确认)。"""
    return {pid for team in parse_lineup(match_html) for pid, _ in team.players}


@dataclass
class RankedTeam:
    rank: Optional[int]
    name: str
    team_id: Optional[str]
    players: list[tuple[str, str]] = field(default_factory=list)  # [(player_id, nick), ...]


def parse_ranking(ranking_html: Html) -> list[RankedTeam]:
    """解析 /ranking/teams(世界排行榜,~226 队):每队名次/队名/team_id/五人阵容。

    这是本地名录的主种子源(2026-07-16 实测:226 块全可解析,含 TYLOO/Lynn Vision;
    每块 `a[href*='/player/']` 去重后即阵容,昵称在 `.nick`)。
    """
    t = tree(ranking_html)
    out: list[RankedTeam] = []
    for b in t.css(".ranked-team"):
        name_el = b.css_first(".ranking-header .name") or b.css_first(".name")
        name = _txt(name_el)
        if not name:
            continue
        rank = None
        pos = b.css_first(".position")
        if pos:
            m = re.search(r"(\d+)", _txt(pos))
            if m:
                rank = int(m.group(1))
        team_id = None
        ta = b.css_first("a[href*='/team/']")
        if ta:
            m = re.search(r"/team/(\d+)/", ta.attributes.get("href", "") or "")
            if m:
                team_id = m.group(1)
        players: list[tuple[str, str]] = []
        seen: set[str] = set()
        for a in b.css("a[href*='/player/']"):
            m = re.search(r"/player/(\d+)/", a.attributes.get("href", "") or "")
            if not m or m.group(1) in seen:
                continue
            nk = a.css_first(".nick")
            nick = _txt(nk)
            if not nick:
                continue
            seen.add(m.group(1))
            players.append((m.group(1), nick))
        out.append(RankedTeam(rank, name, team_id, players))
    return out


@dataclass
class VrsTeam:
    """Valve 世界排名(VRS)总榜上的一支队。"""

    rank: Optional[int]
    name: str
    team_id: Optional[str] = None
    points: Optional[int] = None
    region: str = ""  # EU / AM / AS(HLTV 的大区标)
    players: list[str] = field(default_factory=list)  # 该条目对应阵容的 player_id(2–5 个)


def parse_vrs_ranking(ranking_html: Html) -> tuple[list[VrsTeam], str]:
    """解析 /valve-ranking/teams,返回 (全部队伍, 快照日期文本)。

    DOM 与 /ranking/teams 同源(2026-07-23 实测 389 块,名次/积分/大区/队号/阵容全有):
    `.ranked-team` 每块一队,`.position` = `#12`,`.teamLine` 里依次是 `.name`、
    `.points`(`(2019 Valve points)`,中间夹着 gtSmartphone-only 的 " Valve")、`.region`;
    队号取 `.more` 里的「HLTV Team profile」链接。日期在 `.regional-ranking-header-text`
    (「Valve global ranking on July 23rd, 2026」,尾巴上那个 Beta 角标要去掉)。

    注意两件事:
    1. 这是 **Valve 的 VRS**,与 parse_ranking 的 HLTV 自家世界排名是两套榜,名次不同属正常。
    2. **VRS 排的是阵容不是俱乐部**:同一队号会以两套阵容各占一行(实测 389 行里 36 组重名,
       如 HOTU #36 与 #187、Eternal Fire #97 与 #108),所以每行都带 players,让上层按
       「与该队当前阵容的重合度」挑出真正在役的那条(见 store.upsert_vrs_ranking)。
    """
    t = tree(ranking_html)
    raw_date = re.sub(r"Beta\s*$", "", _txt(t.css_first(".regional-ranking-header-text"))).strip()
    m = re.search(r"ranking on\s+(.+)$", raw_date, re.IGNORECASE)
    date_text = m.group(1).strip() if m else raw_date

    out: list[VrsTeam] = []
    for b in t.css(".ranked-team"):
        name = _txt(b.css_first(".teamLine .name")) or _txt(b.css_first(".name"))
        if not name:
            continue
        rank = None
        if pos := b.css_first(".position"):
            if mm := re.search(r"(\d+)", _txt(pos)):
                rank = int(mm.group(1))
        points = None
        if pt := b.css_first(".teamLine .points"):
            if mm := re.search(r"(\d[\d,]*)", _txt(pt)):
                points = int(mm.group(1).replace(",", ""))
        team_id = None
        if ta := b.css_first("a[href*='/team/']"):
            if mm := re.search(r"/team/(\d+)/", ta.attributes.get("href", "") or ""):
                team_id = mm.group(1)
        players: list[str] = []
        for a in b.css("a[href*='/player/']"):
            if mm := re.search(r"/player/(\d+)/", a.attributes.get("href", "") or ""):
                if mm.group(1) not in players:
                    players.append(mm.group(1))
        out.append(
            VrsTeam(
                rank, name, team_id, points, _txt(b.css_first(".teamLine .region")), players
            )
        )
    return out, date_text


@dataclass
class SearchPlayer:
    id: str
    nick: str
    fullname: str
    team: Optional[str]  # 当前所属队名(可能为 None:无队/自由人)
    team_logo: Optional[str]
    retired: bool = False


@dataclass
class SearchTeam:
    id: str
    name: str
    logo: Optional[str] = None


def parse_search(json_text: str) -> tuple[list[SearchPlayer], list[SearchTeam]]:
    """解析 /search?term= 的 JSON,返回 (players, teams)。

    结构:顶层是 list,元素 0 含 players/teams/events 数组。
    player: {id, nickName, firstName, lastName, team:{name,teamLogoDay,...}|null, retired}
    """
    try:
        data = json.loads(json_text)
    except (TypeError, ValueError):
        return [], []
    root = None
    if isinstance(data, list):
        for el in data:
            if isinstance(el, dict) and ("players" in el or "teams" in el):
                root = el
                break
    elif isinstance(data, dict):
        root = data
    if not isinstance(root, dict):
        return [], []

    players: list[SearchPlayer] = []
    for p in root.get("players") or []:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "").strip()
        nick = str(p.get("nickName") or "").strip()
        if not pid or not nick:
            continue
        team = p.get("team") if isinstance(p.get("team"), dict) else None
        team_name = str(team.get("name")).strip() if team and team.get("name") else None
        team_logo = None
        if team:
            team_logo = team.get("teamLogoDay") or team.get("teamLogoNight")
        full = " ".join(
            x for x in (str(p.get("firstName") or "").strip(), str(p.get("lastName") or "").strip()) if x
        )
        players.append(
            SearchPlayer(pid, nick, full, team_name, team_logo, bool(p.get("retired")))
        )

    teams: list[SearchTeam] = []
    for tm in root.get("teams") or []:
        if not isinstance(tm, dict):
            continue
        tid = str(tm.get("id") or "").strip()
        name = str(tm.get("name") or "").strip()
        if not tid or not name:
            continue
        teams.append(SearchTeam(tid, name, tm.get("teamLogoDay") or tm.get("teamLogoNight")))
    return players, teams
