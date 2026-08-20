"""把战报/赛程/赛事渲染成 PNG(htmlrender → Chromium 截图)。

三张卡片统一采用「暖米色」设计稿风格:
米色页面(#E9E5DC)托起一张略偏白的圆角卡(#FAF9F5),赤陶色(#C4704E)为强调色。
- 赛果卡:赛事/阶段/BOx、系列赛进程(BO3/5,地图 chip)、本图比分+半场、两队各 5 人 Rating。
- 赛程卡:今日比赛(已结束/进行中/待开始),按赛事分组;已结束显示赛果(胜绿负红)。
- 赛事卡:未来 3 个月顶级赛事列表。
渲染成图片,固定浅色主题,无需适配深色模式。评分 ≥1.0 绿、<1.0 红(与设计稿一致)。

赛程/日程卡的队名旁跟一个 `#N` = Valve 世界排名(VRS),名次由调用方传进来(见 _vrs_tag);
战报卡/开赛卡不加,那两张已有整块 VRS 面板(_vrs_panel),再标一遍是重复。
"""

from __future__ import annotations

import asyncio
import base64
import html as _html
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from . import majors, store
from .hltv import (
    Bracket,
    BracketRound,
    EventDetail,
    EventSchedule,
    GroupStanding,
    LineupTeam,
    MapResult,
    MatchDetail,
    Matchup,
    PlayerStat,
    ScheduledMatch,
    SlotTeam,
    SwissStage,
    VrsCell,
    VrsRow,
    cluster_match_days,
)

CN_TZ = ZoneInfo("Asia/Shanghai")

# —— 卡片西文字体(可变字体,base64 内嵌到 @font-face,不依赖系统/网络)——
# 内嵌是渲染真正生效的保证(不依赖 Chromium 能否读到系统字体);中文无对应字形,
# 自动回退 Noto Sans SC / PingFang SC。
# 默认用仓库自带的 Hanken Grotesk(设计稿指定字体,SIL OFL 可再分发,许可见同目录
# OFL.txt);想换别的字体,把任意一个 .ttf 丢进 assets/fonts/local/(已 gitignore,
# 不会被提交)即可覆盖默认——按文件名排序取第一个。
_FONT_DIR = Path(__file__).with_name("assets") / "fonts"


def _font_file() -> Path:
    local = sorted((_FONT_DIR / "local").glob("*.ttf"))
    return local[0] if local else _FONT_DIR / "HankenGrotesk-Variable.ttf"


@lru_cache(maxsize=1)
def _font_face() -> str:
    """把西文可变字体读一次、base64 内嵌成 @font-face;缺文件则空串(回退系统字体)。"""
    try:
        data = base64.b64encode(_font_file().read_bytes()).decode()
    except OSError:
        return ""
    return (
        "@font-face{font-family:'CS2 Card Sans';"
        f"src:url(data:font/ttf;base64,{data}) format('truetype');"
        "font-weight:100 900;font-style:normal;font-display:swap;}"
    )


# —— 设计稿配色(暖米色主题)——
PAGE = "#E9E5DC"  # 页面背景(卡片四周留白,衬出投影)
CARD = "#FAF9F5"  # 卡片表面
PANEL = "#F0EDE5"  # 系列赛面板 / 表头底
INNER = "#FDFCF9"  # 内层小卡(地图 chip / 评分表)底
INNER2 = "#F4F1EA"  # 待定 chip 底
INK = "#1F1C17"  # 主文字(暖近黑)
ROW = "#38352E"  # 选手名等正文
DARK = "#26231C"  # 深色圆徽(事件/队占位)
DARK2 = "#2A251C"  # 深色小圆徽
SUB = "#7A756A"  # 次级文字
MUTE = "#8C8578"  # 更淡标签
FAINT = "#A8A296"  # 最淡(半场/脚注)
FAINT2 = "#9B9488"  # 待定队名 / 落后比分
FAINT3 = "#B4AEA1"  # 待定值 / 破折号
ACCENT = "#C4704E"  # 赤陶主色
ACCENT_RGB = "196,112,78"  # ACCENT 的 rgb 分量,供胜方半透明渐变叠层用
ACCENT_D = "#A85638"  # 深赤陶(胜方文字)
ACCENT_BG = "#F3E8E1"  # 浅赤陶底(胜方高亮)
GOOD = "#5E7052"  # 好评分(橄榄绿)/ 晋级
BAD = "#BB5A40"  # 差评分(赤红)/ 淘汰
WIN_C = "#9C4529"  # WIN 文字:胜方表头赤陶底(ACCENT_BG)加深
LOSE_C = "#615A4C"  # LOSE 文字:负方表头米灰底(PANEL)加深
GOOD_BG = "rgba(94,112,82,0.11)"  # 晋级带底(浅橄榄绿)
GOOD_BD = "rgba(94,112,82,0.30)"  # 晋级带边
BAD_BG = "rgba(187,90,64,0.09)"  # 淘汰带底(浅赤红)
BAD_BD = "rgba(187,90,64,0.28)"  # 淘汰带边
STAR = "#C8963A"  # Major 冠军星标(暖金,几冠几颗)
BORDER = "rgba(31,28,23,0.08)"
BORDER_R = "rgba(31,28,23,0.07)"  # 行分隔线
BORDER_S = "rgba(31,28,23,0.06)"  # 更浅分隔线
LINE = "rgba(31,28,23,0.1)"  # 表头下的主分隔线
# 拉丁/数字用内嵌西文字体(见 _font_file);中文无对应字形,自动回退 Noto Sans SC / PingFang SC
FONT = (
    '"CS2 Card Sans","Noto Sans SC",-apple-system,BlinkMacSystemFont,'
    '"PingFang SC","Helvetica Neue",sans-serif'
)

_SHADOW = "0 1px 0 rgba(255,255,255,0.6) inset,0 24px 50px -30px rgba(31,28,23,0.32)"

# 渲染结果内存缓存:HTML 没变就不必再截一次图(重复命令、多个群同问,直接秒回)。
# key = sha1(html),值 = (时间戳, png)。容量/时效都很小,防的是重复渲染不是持久化。
_PNG_CACHE: dict[str, tuple[float, bytes]] = {}
_PNG_TTL = 600
_PNG_MAX = 32
# 正在渲染中的 key → future,用于并发去重(见 _render_png)
_PNG_INFLIGHT: dict[str, "asyncio.Future[bytes]"] = {}


def _render_request(html: str):
    """构造 htmlrender 请求。

    **必须显式给 wait_until**:传裸 str 时 htmlrender 建的 ContentConfig 用默认值
    ``networkidle``,而 Playwright 的 networkidle = 「500ms 内无网络连接」。我们的卡
    片字体和队标全是 data: URI、一个网络请求都不发,那 500ms 是纯等待——每张卡、
    每个群、每次直播推送都白付。改 ``load`` 后立刻可截图。
    """
    from nonebot_plugin_htmlrender.backend.playwright.models import (
        ContentConfig,
        HtmlRenderRequest,
        PageConfig,
        PngScreenshotOptions,
        RenderConfig,
        ViewportConfig,
    )

    return HtmlRenderRequest(
        content=ContentConfig(html=html, wait_until="load"),
        render=RenderConfig(
            page=PageConfig(viewport=ViewportConfig(width=1320, height=10)),
            screenshot=PngScreenshotOptions(device_scale_factor=2, full_page=True),
        ),
    )


async def _render_png(html: str) -> bytes:
    import hashlib
    import time as _time

    from nonebot_plugin_htmlrender import render_html

    key = hashlib.sha1(html.encode()).hexdigest()
    # 上一张卡的 logo utime 去重到此为止(无论这次命不命中缓存都要重置,否则
    # 连续命中时集合只涨不清,后续卡的 logo 就再也不会被 touch 到)。
    store.end_render_touches()
    hit = _PNG_CACHE.get(key)
    now = _time.time()
    if hit and now - hit[0] < _PNG_TTL:
        return hit[1]

    # 同一张卡并发请求(多个群同时问 / 直播推送撞上手动命令)只截一次图:
    # 后到者等前一个的 future,不再各开一个 Chromium page 重跑整轮渲染。
    inflight = _PNG_INFLIGHT.get(key)
    if inflight is not None:
        return await asyncio.shield(inflight)

    fut: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
    _PNG_INFLIGHT[key] = fut
    try:
        png = await render_html(_render_request(html))
    except BaseException as exc:
        if not fut.done():
            fut.set_exception(exc)
        # 没人 await 这个 future 时,避免 asyncio 打印 "exception was never retrieved"
        fut.exception()
        raise
    finally:
        _PNG_INFLIGHT.pop(key, None)
    if not fut.done():
        fut.set_result(png)

    if len(_PNG_CACHE) >= _PNG_MAX:
        oldest = min(_PNG_CACHE, key=lambda k: _PNG_CACHE[k][0])
        _PNG_CACHE.pop(oldest, None)
    _PNG_CACHE[key] = (now, png)
    return png


def _esc(s: str) -> str:
    return _html.escape(s or "")


def _rating_color(r: float) -> str:
    # 设计稿:1.0 为基准线,≥1.0 绿、<1.0 红(样例 1.01 绿 / 0.81 红)。
    return GOOD if r >= 1.0 else BAD


# ———————————————————— 通用:外壳 + 徽标 ————————————————————
def _shell(body: str, pad: str) -> str:
    """米色页面 + 圆角卡外壳;pad = 卡片内边距(如 "44px 48px 40px")。

    font-family 必须写在 <style> 里而非内联 style="...":字体栈里的双引号会把
    双引号包裹的 style 属性提前截断,导致整个字体声明失效(回退衬线体)。"""
    return f"""<style>{_font_face()}
*{{margin:0;padding:0;box-sizing:border-box;}}
html,body{{background:{PAGE};}}
body{{font-family:{FONT};}}
</style>
<div style="background:{PAGE};padding:56px 60px;display:flex;justify-content:center;
     color:{INK};-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;">
 <div style="width:1200px;background:{CARD};border:1px solid {BORDER};border-radius:24px;
      box-shadow:{_SHADOW};padding:{pad};">
{body}
 </div>
</div>"""


def _logo_plain(url: Optional[str], size: int) -> Optional[str]:
    """有 logo 就透明直贴(不加底色);无则 None。多数队标透明背景,直接贴在卡上更干净。

    纯白队标(Spirit、EYEBALLERS 等)贴米色卡面看不见的问题,已在解析层解决:
    hltv._pick_logo 优先取 HLTV 的 day-only(浅底)变体,纯白队标据此取到深色版,
    故这里无需再描边/加底,保持干净直贴即可。"""
    uri = store.logo_data_uri(url)
    if not uri:
        return None
    return (
        f'<img src="{uri}" width="{size}" height="{size}" '
        f'style="object-fit:contain;display:block;" alt="">'
    )


def _circle(text: str, size: int, bg: str, fg: str) -> str:
    """彩色圆形首字母徽标(无 logo 时的回退,保留设计稿的圆徽节奏)。"""
    initials = _esc((text or "?")[:1].upper())
    fs = max(9, round(size * 0.42))
    return (
        f'<div style="width:{size}px;height:{size}px;border-radius:50%;background:{bg};'
        f"color:{fg};display:inline-flex;align-items:center;justify-content:center;"
        f'font-size:{fs}px;font-weight:700;flex:none;">{initials}</div>'
    )


def _badge(url: Optional[str], text: str, size: int, bg: str, fg: str) -> str:
    """logo 优先(透明直贴、居中于 size 方框),否则彩色圆首字母。"""
    img = _logo_plain(url, size)
    if img:
        return (
            f'<span style="width:{size}px;height:{size}px;display:inline-flex;'
            f'align-items:center;justify-content:center;flex:none;">{img}</span>'
        )
    return _circle(text, size, bg, fg)


# Major 冠军星标:数据来自 majors.py(静态名录,半年一更),几冠画几颗星。
# 用内嵌 SVG 而不是「★」字符——西文字体没有该字形会回退到中文字体,
# 大小/基线/颜色都不可控;SVG 则尺寸精确、颜色统一。
_STAR_PATH = "M12 2.6l2.95 5.98 6.6.96-4.78 4.66 1.13 6.57L12 17.67l-5.9 3.1 1.13-6.57L2.45 9.54l6.6-.96z"


def _stars(n: int, size: int = 11, side: str = "right") -> str:
    """n 座 Major 冠军 → n 颗金星;n<=0 返回空串(没拿过就什么都不加)。
    side="left" 用于右对齐的一侧(星在名字左边,间距挪到右侧)。"""
    if n <= 0:
        return ""
    star = (
        f'<svg viewBox="0 0 24 24" width="{size}" height="{size}" fill="{STAR}" '
        f'style="display:block;flex:none;"><path d="{_STAR_PATH}"/></svg>'
    )
    margin = "margin-right:6px;" if side == "left" else "margin-left:6px;"
    return (
        f'<span style="display:inline-flex;align-items:center;gap:1.5px;flex:none;'
        f'{margin}">{star * n}</span>'
    )


# ———————————————————— Valve 世界排名(VRS)角标 ————————————————————
# 日程卡/赛程卡上凡是出现队名的地方都跟一个 `#12`。名次由调用方(命令处理器)从本地
# vrs_ranking 表整表读出后传进来 —— 渲染层不查库、更不抓 HLTV。
# 用 ContextVar 而不是层层传参:队名藏在 6 层嵌套的组件里(赛程卡:
# build → _bracket_section → _bracket_row → _round_grid → _round_col → _bracket_cell →
# _mu_team_row),透传会把每个组件的签名都污染一遍。build_*_html 是纯同步的(中途没有
# await),ContextVar 在这里既够用又不会被并发渲染串味。
_VRS_RANKS: ContextVar[Optional[dict[str, int]]] = ContextVar("cs2_vrs_ranks", default=None)


@contextmanager
def _vrs_scope(ranks: Optional[dict[str, int]]):
    token = _VRS_RANKS.set(ranks or None)
    try:
        yield
    finally:
        _VRS_RANKS.reset(token)


def _vrs_rank(name: str) -> Optional[int]:
    ranks = _VRS_RANKS.get()
    return ranks.get((name or "").strip().casefold()) if ranks else None


def _vrs_tag(name: str, size: float = 12.5, dim: bool = False) -> str:
    """队名旁的 `#12` 角标;查不到名次(未上榜/表还没建好)就什么都不加。"""
    rank = _vrs_rank(name)
    if not rank:
        return ""
    color = FAINT3 if dim else MUTE
    return (
        f'<span style="flex:none;font-size:{size}px;font-weight:600;color:{color};'
        f'font-variant-numeric:tabular-nums;letter-spacing:-0.2px;">#{rank}</span>'
    )


def _dim(html: str) -> str:
    """把队标置灰调浅——表示已结束比赛的败者。日程/赛程/战报卡统一用此处理。"""
    return (
        f'<span style="display:inline-flex;flex:none;filter:grayscale(100%);'
        f'opacity:0.4;">{html}</span>'
    )


def _result_badge(url: Optional[str], text: str, size: int, won: bool) -> str:
    """比分两侧的队标:胜者正常显示,败者置灰调浅(灰度 + 降透明度)以示落败。"""
    badge = _badge(url, text, size, ACCENT if won else DARK2, CARD)
    return badge if won else _dim(badge)


# ———————————————————— 胜方赤陶渐变叠层(已结束比赛) ————————————————————
# 已结束比赛:负方队标置灰(见 _dim),胜方一侧再叠一层赤陶渐变背景——实色落在胜方
# 队标/队名处,朝外侧「像素化消散」(参考设计稿的像素渐变质感)。做法:一层平滑赤陶
# 铺底 + 一层棋盘遮罩的像素点,像素点用 mask「棋盘 ∩ 方向渐变」(intersect)只出现在
# 消散带。CSS mask 由 htmlrender 的 Chromium 渲染;叠层绝对定位、z-index:0,压在内容之下。
_CHECK = "conic-gradient(#000 0 25%,#0000 0 50%,#000 0 75%,#0000 0)"  # 4px 棋盘 → 像素颗粒


def _win_overlay(
    anchor: str,
    width: str,
    fade: str,
    wash: str,
    band: str,
    dot_a: float = 0.5,
    px: int = 7,
    inset: str = "top:0;bottom:0;",
) -> str:
    """胜方渐变叠层。anchor=贴边("left:0;"/"right:0;")、width=覆盖宽度、fade=渐变方向;
    wash=平滑铺底的色标串,band=限定像素点范围的遮罩色标串;px=棋盘格边长(行越矮要越小,
    否则颗粒糊成色块),inset=上下贴边方式。须放在 position:relative 的容器内,容器内容
    另置于 z-index:1 之上。"""
    smooth = (
        f'<div style="position:absolute;inset:0;background:linear-gradient({fade},{wash});"></div>'
    )
    pixels = (
        f'<div style="position:absolute;inset:0;background:rgba({ACCENT_RGB},{dot_a});'
        f"-webkit-mask-image:{_CHECK},linear-gradient({fade},{band});"
        f"-webkit-mask-size:{px}px {px}px,100% 100%;-webkit-mask-repeat:repeat,no-repeat;"
        f"-webkit-mask-composite:source-in;"
        f"mask-image:{_CHECK},linear-gradient({fade},{band});"
        f'mask-size:{px}px {px}px,100% 100%;mask-repeat:repeat,no-repeat;'
        f'mask-composite:intersect;"></div>'
    )
    return (
        f'<div style="position:absolute;{inset}{anchor}width:{width};z-index:0;'
        f'pointer-events:none;">{smooth}{pixels}</div>'
    )


# 叠层的内缘必须**贴住比分列的外沿**,不能简单按行宽 50% 贴边——比分列并不在行的正中:
# 两种行的最左/最右列都不等宽,比分列因此整体右偏,50% 的边界会同时错开左右两侧(右方获胜
# 时渐变压到比分数字上、左方获胜时又够不到队标,用户报的错位)。用 calc 从行中线推到比分列
# 边界即可对齐,偏移量由各自的列模板算出(推导见下面两个函数)。色标也一律改用 px 而非 %:
# 内缘对齐后左右两个盒子宽度天然不等,再用 % 会让浓度带落在不同距离上,又是一种错位。
def _win_wash_sched(side: str) -> str:
    """日程行:胜方队标/队名处最浓,朝外侧(时间/状态侧)像素消散;比分数字一侧留干净。

    列模板:外层 ``74px | 1fr | 54px``,中层 ``1fr 24px 64px 24px 1fr`` gap 12。
    记行宽 W,则比分列 = ``W/2-22 … W/2+42``(中层被两侧 74/54 的差挤得右偏 10px),
    故左叠层宽 ``50%-22px``、右叠层宽 ``50%-42px``;两侧队标都落在离内缘 12–36px 处。
    """
    anchor = "left:0;" if side == "left" else "right:0;"
    fade = "to left" if side == "left" else "to right"  # 0=内侧(比分)→ 外侧
    width = "calc(50% - 22px)" if side == "left" else "calc(50% - 42px)"
    a = ACCENT_RGB
    wash = f"rgba({a},0) 0,rgba({a},0.19) 22px,rgba({a},0.19) 180px,rgba({a},0) 430px"
    band = "transparent 100px,#000 250px,transparent 440px"
    return _win_overlay(anchor, width, fade, wash, band, dot_a=0.42)


def _win_wash_recap(side: str) -> str:
    """战报摘要行:与 _win_wash_sched 同一套语言(胜方一侧起、朝外侧像素消散),但行高只有
    主排期的一半 —— 棋盘缩到 5px 才不会糊成整块色斑,浓度压低一档以免抢走下方主排期的
    视觉重量;上下各留 2px,让相邻两行同侧获胜时不连成一大片。

    列模板 ``56px | 1fr | 44px | 1fr`` gap 10:比分列 = ``W/2+11 … W/2+55``(右偏 11px),
    故左叠层宽 ``50%+11px``、右叠层宽 ``50%-55px``;两侧队标都落在离内缘 10–28px 处。
    """
    anchor = "left:0;" if side == "left" else "right:0;"
    fade = "to left" if side == "left" else "to right"
    width = "calc(50% + 11px)" if side == "left" else "calc(50% - 55px)"
    a = ACCENT_RGB
    wash = f"rgba({a},0) 0,rgba({a},0.16) 14px,rgba({a},0.16) 150px,rgba({a},0) 380px"
    band = "transparent 80px,#000 210px,transparent 390px"
    return _win_overlay(
        anchor, width, fade, wash, band, dot_a=0.34, px=5, inset="top:2px;bottom:2px;"
    )


def _win_wash_swiss(side: str) -> str:
    """瑞士轮小卡:胜方队标(外缘)最浓,朝中间比分像素消散。"""
    anchor = "left:0;" if side == "left" else "right:0;"
    fade = "to right" if side == "left" else "to left"  # 实色在外缘 → 向中间消散
    a = ACCENT_RGB
    wash = f"rgba({a},0.2) 0%,rgba({a},0) 60%"
    band = "transparent 12%,#000 34%,transparent 62%"
    return _win_overlay(anchor, "54%", fade, wash, band)


def _win_wash_row() -> str:
    """淘汰赛胜方整行:从队标(左)向比分(右)像素消散。"""
    a = ACCENT_RGB
    wash = f"rgba({a},0.2) 0%,rgba({a},0) 70%"
    band = "transparent 16%,#000 40%,transparent 74%"
    return _win_overlay("left:0;", "100%", "to right", wash, band)


def _pick_tag(match: "MatchDetail", mp: MapResult, size: int = 16, color: str = MUTE) -> str:
    """地图归属标记:选图方队标 + "Picked";决胜图用赛事 logo + "Decider"。
    BO1 / 未知(picked_by 为空)→ 空串。"""
    if not mp.picked_by:
        return ""
    if mp.picked_by == "decider":
        logo, name, label = match.event_logo, match.event_name, "Decider"
    else:
        logo = match.team1_logo if mp.picked_by == "team1" else match.team2_logo
        name = match.team1 if mp.picked_by == "team1" else match.team2
        label = "Picked"
    badge = _badge(logo, name, size, DARK2, CARD)
    fs = max(11, round(size * 0.72))
    return (
        f'<span style="display:inline-flex;align-items:center;gap:5px;flex:none;">{badge}'
        f'<span style="font-size:{fs}px;color:{color};font-weight:600;'
        f'letter-spacing:0.2px;">{label}</span></span>'
    )


# ———————————————————— 通用:VRS(Valve 世界排名)面板 ————————————————————
# 战报卡(每张小场)与开赛卡共用。数据来自比赛页的 VRS 面板(hltv.parse_vrs):
# 赛前是「当前 / 若获胜 / 若失利」三档预测,系列赛结束后 HLTV 自己换成实际增减。
def _vrs_num(c: Optional[VrsCell]) -> str:
    """一格:积分(增减带正负号并上色)+ 名次 pill(带名次变化)。"""
    if c is None:
        return f'<span style="font-size:17px;color:{FAINT3};">—</span>'
    color = INK
    if c.signed:
        color = GOOD if c.points.startswith("+") else BAD
    pts = (
        f'<span style="font-size:19px;font-weight:700;color:{color};letter-spacing:-0.2px;'
        f'font-variant-numeric:tabular-nums;">{_esc(c.points)}</span>'
        if c.points
        else ""
    )
    pill = ""
    if c.rank:
        up = c.rank_delta is not None and c.rank_delta > 0
        down = c.rank_delta is not None and c.rank_delta < 0
        bg, fg = (GOOD_BG, GOOD) if up else (BAD_BG, BAD) if down else (INNER2, SUB)
        delta = (
            f'<span style="font-size:12px;font-weight:600;margin-left:4px;">'
            f"{c.rank_delta:+d}</span>"
            if (up or down)
            else ""
        )
        pill = (
            f'<span style="display:inline-flex;align-items:center;background:{bg};color:{fg};'
            f"font-size:13px;font-weight:600;padding:3px 9px;border-radius:999px;"
            f'font-variant-numeric:tabular-nums;flex:none;">#{c.rank}{delta}</span>'
        )
    return (
        f'<span style="display:inline-flex;align-items:center;gap:8px;">{pts}{pill}</span>'
    )


def _vrs_panel(match: MatchDetail) -> str:
    """VRS 面板;比赛页没有该模块(低级别赛事/未上榜)时返回空串,卡片自动不显示。"""
    v = match.vrs
    if v is None:
        return ""
    pair = v.pair(match.team1, match.team2)
    if not pair:
        return ""
    row1, row2 = pair
    winner = None
    if match.series_over and match.series1 != match.series2:
        winner = "team1" if match.series1 > match.series2 else "team2"

    if v.settled:  # HLTV 已结算:左列=赛前,中列=本场实际增减
        title, note = "VRS 赛果", "本场对 Valve 世界排名的影响"
        labels = ["赛前", "赛果"]

        def cells(r: VrsRow, won: bool) -> list:
            return [r.current, r.win]

    elif winner:  # 系列赛已结束但 HLTV 还没结算 → 取预测里与胜负对应的那一档
        title, note = "VRS 赛果", "HLTV 尚未结算,按本场胜负取预测值"
        labels = ["赛前", "赛果 · 预计"]

        def cells(r: VrsRow, won: bool) -> list:
            return [r.current, r.win if won else r.lose]

    else:  # 赛前/进行中:三档预测
        title, note = "VRS 预测", "本场对 Valve 世界排名的预计影响"
        labels = ["当前", "若获胜", "若失利"]

        def cells(r: VrsRow, won: bool) -> list:
            return [r.current, r.win, r.lose]

    head = "".join(
        f'<div style="flex:1;text-align:center;font-size:13px;color:{MUTE};'
        f'font-weight:600;letter-spacing:0.3px;">{lb}</div>'
        for lb in labels
    )

    def team_row(row: VrsRow, name: str, logo: Optional[str], won: bool, first: bool) -> str:
        top = "" if first else f"border-top:1px solid {BORDER_S};"
        vals = "".join(
            f'<div style="flex:1;display:flex;align-items:center;justify-content:center;">'
            f"{_vrs_num(c)}</div>"
            for c in cells(row, won)
        )
        return (
            f'<div style="display:flex;align-items:center;padding:13px 20px;{top}">'
            f'<div style="flex:1.5;display:flex;align-items:center;gap:11px;min-width:0;">'
            f"{_badge(logo, name, 24, DARK2, CARD)}"
            f'<span style="font-size:17px;color:{INK};font-weight:500;overflow:hidden;'
            f'text-overflow:ellipsis;white-space:nowrap;">{_esc(name)}</span></div>'
            f"{vals}</div>"
        )

    return (
        f'<div style="margin-top:24px;border:1px solid {BORDER};border-radius:16px;'
        f'overflow:hidden;background:{INNER};">'
        f'<div style="display:flex;align-items:center;padding:12px 20px;background:{PANEL};'
        f'border-bottom:1px solid {BORDER_R};">'
        f'<div style="flex:1.5;min-width:0;display:flex;align-items:baseline;gap:9px;">'
        f'<span style="font-size:15px;font-weight:700;color:{INK};letter-spacing:0.2px;">{title}</span>'
        f'<span style="font-size:12px;color:{MUTE};overflow:hidden;text-overflow:ellipsis;'
        f'white-space:nowrap;">{note}</span></div>{head}</div>'
        f'{team_row(row1, match.team1, match.team1_logo, winner == "team1", True)}'
        f'{team_row(row2, match.team2, match.team2_logo, winner == "team2", False)}'
        f"</div>"
    )


# ———————————————————— 赛果卡(战报) ————————————————————
def _score_team(name: str, badge: str, side: str) -> str:
    """大比分面板两侧的队伍:队标 + Major 冠军星。星在**外侧**(左队在左、右队在右),
    与队标同一行居中——不挤中间的大比分。队标 52px,星相应放大到 20px。"""
    stars = _stars(majors.team_titles(name), 20, side)
    inner = f"{stars}{badge}" if side == "left" else f"{badge}{stars}"
    return f'<span style="display:flex;align-items:center;flex:none;">{inner}</span>'


def _score_panel(
    caption: str, b1: str, s1: int, s2: int, c1: str, c2: str, b2: str, chips: str = ""
) -> str:
    """居中面板:小标题 + 「队标 大比分 队标」;chips 非空时下方再排地图 chip 行。
    BO1 赛果与 BO3/5 系列赛进程共用此版式(居中、放大)。"""
    chips_html = (
        f'<div style="display:flex;gap:12px;margin-top:22px;">{chips}</div>' if chips else ""
    )
    return (
        f'<div style="margin-top:26px;background:{PANEL};border:1px solid {BORDER};'
        f'border-radius:18px;padding:22px 24px;">'
        f'<div style="display:flex;flex-direction:column;align-items:center;gap:12px;">'
        f'<div style="font-size:15px;color:{MUTE};font-weight:500;letter-spacing:0.2px;">{caption}</div>'
        f'<div style="display:flex;align-items:center;justify-content:center;gap:26px;">{b1}'
        f'<span style="font-size:46px;font-weight:700;font-variant-numeric:tabular-nums;letter-spacing:1px;">'
        f'<span style="color:{c1};">{s1}</span> <span style="color:{FAINT3};">–</span> '
        f'<span style="color:{c2};">{s2}</span></span>{b2}</div></div>'
        f"{chips_html}</div>"
    )


def _series_panel(match: MatchDetail, map_index: int, s1: int, s2: int) -> str:
    if match.best_of <= 1:
        return ""
    b1 = _score_team(
        match.team1, _badge(match.team1_logo, match.team1, 52, ACCENT if s1 > s2 else DARK2, CARD), "left"
    )
    b2 = _score_team(
        match.team2, _badge(match.team2_logo, match.team2, 52, ACCENT if s2 > s1 else DARK2, CARD), "right"
    )
    c1 = ACCENT if s1 > s2 else FAINT2
    c2 = ACCENT if s2 > s1 else FAINT2

    def _chip_head(name: str, name_style: str, pick: str) -> str:
        # 图名一行:名字靠左、选图标记("[队标] picked")靠右
        return (
            f'<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;">'
            f'<span style="{name_style}min-width:0;overflow:hidden;text-overflow:ellipsis;'
            f'white-space:nowrap;">{name}</span>{pick}</div>'
        )

    chips = ""
    for i, mp in enumerate(match.maps):
        cur = i == map_index
        name = _esc(mp.name) or "?"
        if mp.finished and mp.winner:
            # 比分两侧各贴对应队标(左=team1,右=team2),败者队标置灰调浅
            bl = _result_badge(match.team1_logo, match.team1, 16, mp.winner == "team1")
            br = _result_badge(match.team2_logo, match.team2, 16, mp.winner == "team2")
            c_s1 = ACCENT_D if mp.winner == "team1" else FAINT2
            c_s2 = ACCENT_D if mp.winner == "team2" else FAINT2

            def _score_row(
                base_color: str,
                weight: str,
                bl: str = bl,
                br: str = br,
                mp: MapResult = mp,
                c_s1: str = c_s1,
                c_s2: str = c_s2,
            ) -> str:
                # 胜方分数用强调色,败方分数调浅;整体基色随 chip 状态(当前图/普通)
                return (
                    f'<div style="display:flex;align-items:center;gap:8px;">{bl}'
                    f'<span style="font-size:14px;{weight}font-variant-numeric:tabular-nums;color:{base_color};">'
                    f'<span style="color:{c_s1};">{mp.team1_score}</span>'
                    f'<span style="color:{FAINT3};"> – </span>'
                    f'<span style="color:{c_s2};">{mp.team2_score}</span></span>{br}</div>'
                )

            if cur:
                head = _chip_head(
                    name,
                    f"font-size:15px;font-weight:700;color:{ACCENT_D};",
                    _pick_tag(match, mp, 15, ACCENT_D),
                )
                chips += (
                    f'<div style="flex:1;min-width:0;border:1.5px solid {ACCENT};border-radius:14px;'
                    f'background:{ACCENT_BG};padding:14px;display:flex;flex-direction:column;gap:9px;">'
                    f"{head}{_score_row(SUB, 'font-weight:600;')}</div>"
                )
            else:
                head = _chip_head(
                    name,
                    f"font-size:15px;font-weight:600;color:{INK};",
                    _pick_tag(match, mp, 15, MUTE),
                )
                chips += (
                    f'<div style="flex:1;min-width:0;border:1px solid {BORDER};border-radius:14px;'
                    f'background:{INNER};padding:14px;display:flex;flex-direction:column;gap:9px;">'
                    f"{head}{_score_row(SUB, '')}</div>"
                )
        else:
            head = _chip_head(
                name,
                f"font-size:15px;font-weight:600;color:{FAINT2};",
                _pick_tag(match, mp, 15, FAINT2),
            )
            chips += (
                f'<div style="flex:1;min-width:0;border:1px solid {BORDER};border-radius:14px;'
                f'background:{INNER2};padding:14px;display:flex;flex-direction:column;gap:9px;">'
                f"{head}"
                f'<div style="font-size:14px;color:{FAINT3};">待定</div></div>'
            )

    return _score_panel(f"系列赛进程 · BO{match.best_of}", b1, s1, s2, c1, c2, b2, chips)


def _bo1_result_panel(match: MatchDetail, m: MapResult) -> str:
    """BO1 没有系列赛进程,用同款「居中大比分 + 两队队标」面板展示本图赛果。"""
    s1 = m.team1_score if m.team1_score is not None else 0
    s2 = m.team2_score if m.team2_score is not None else 0
    t1w, t2w = (m.winner == "team1"), (m.winner == "team2")
    b1 = _score_team(
        match.team1, _badge(match.team1_logo, match.team1, 52, ACCENT if t1w else DARK2, CARD), "left"
    )
    b2 = _score_team(
        match.team2, _badge(match.team2_logo, match.team2, 52, ACCENT if t2w else DARK2, CARD), "right"
    )
    c1 = ACCENT if t1w else FAINT2
    c2 = ACCENT if t2w else FAINT2
    cap = f"{_esc(m.name)} · BO{match.best_of}" if m.name else f"比赛结果 · BO{match.best_of}"
    return _score_panel(cap, b1, s1, s2, c1, c2, b2)


def build_card_html(match: MatchDetail, map_index: int, when_text: str) -> str:
    m = match.maps[map_index]
    upto = [x for x in match.maps[: map_index + 1] if x.finished]
    s1 = sum(1 for x in upto if x.winner == "team1")
    s2 = sum(1 for x in upto if x.winner == "team2")

    parts = []
    if match.stage:
        parts.append(_esc(match.stage))
    parts.append(f"BO{match.best_of}")
    parts.append("LAN" if match.is_lan else "线上")
    subtitle = " · ".join(parts)

    event_badge = _badge(match.event_logo, match.event_name, 60, DARK, CARD)

    # 本图小结:胜方队名加重(近黑)、比分赤陶;负方灰。
    t1_win = m.winner == "team1"
    t2_win = m.winner == "team2"
    n1 = f"font-weight:600;color:{INK};" if t1_win else f"color:{SUB};"
    n2 = f"font-weight:600;color:{INK};" if t2_win else f"color:{SUB};"
    sc1 = f"color:{ACCENT};" if t1_win else f"color:{SUB};"
    sc2 = f"color:{ACCENT};" if t2_win else f"color:{SUB};"
    map_no = f"第 {map_index + 1} 张" if match.best_of > 1 else "单图"
    half = (
        f'<div style="font-size:14px;color:{FAINT};margin-top:5px;">半场 {_esc(m.halves)}</div>'
        if m.halves
        else ""
    )

    # 顶部结果面板:BO3/5 = 系列赛进程大比分;BO1 = 本图赛果(同款居中大版式)
    result_panel = (
        _bo1_result_panel(match, m)
        if match.best_of <= 1
        else _series_panel(match, map_index, s1, s2)
    )
    # 本图小结右侧的「队名+本图比分」:BO1 已在上方大面板展示,避免重复 → 省略
    map_result = (
        (
            f'<div style="font-size:21px;font-variant-numeric:tabular-nums;letter-spacing:-0.2px;text-align:right;">'
            f'<span style="{n1}">{_esc(match.team1)}</span> <span style="font-weight:700;{sc1}">{m.team1_score}</span>'
            f'<span style="color:{FAINT3};"> – </span>'
            f'<span style="font-weight:700;{sc2}">{m.team2_score}</span> <span style="{n2}">{_esc(match.team2)}</span>'
            f"</div>"
        )
        if match.best_of > 1
        else ""
    )

    body = f"""
  <div style="display:flex;align-items:center;gap:18px;">
   {event_badge}
   <div style="display:flex;flex-direction:column;gap:3px;min-width:0;">
    <div style="font-size:30px;font-weight:700;color:{INK};letter-spacing:-0.4px;line-height:1.1;">{_esc(match.event_name) or "CS2 比赛"}</div>
    <div style="font-size:17px;color:{SUB};font-weight:500;">{subtitle}</div>
   </div>
  </div>

  {result_panel}

  <div style="display:flex;align-items:flex-start;justify-content:space-between;margin-top:26px;padding:0 2px;">
   <div>
    <div style="display:flex;align-items:center;gap:11px;">
     <span style="font-size:15px;color:{MUTE};font-weight:500;">{map_no}</span>
     <span style="font-size:21px;font-weight:600;color:{INK};letter-spacing:-0.2px;">{_esc(m.name)}</span>
     {_pick_tag(match, m, 18, MUTE)}
    </div>
    {half}
   </div>
   {map_result}
  </div>

  <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:24px;">
   {_rating_table(match.team1, match.team1_logo, m.team1_players, ("win" if t1_win else "lose") if m.winner else None)}
   {_rating_table(match.team2, match.team2_logo, m.team2_players, ("win" if t2_win else "lose") if m.winner else None)}
  </div>

  {_vrs_panel(match)}

  <div style="display:flex;align-items:center;justify-content:space-between;margin-top:30px;
       padding-top:20px;border-top:1px solid {BORDER};">
   <div style="font-size:14px;color:{FAINT};">{_esc(when_text)}</div>
   <div style="font-size:14px;color:{MUTE};">HLTV · <span style="color:{ACCENT};font-weight:600;">hltv.org</span></div>
  </div>"""
    return _shell(body, "44px 48px 40px")


def _rating_table(
    team: str, logo: Optional[str], players: list[PlayerStat], result: Optional[str] = None
) -> str:
    """result: "win" / "lose" / None(未决)。胜方高亮 + 绿 WIN 徽标,负方红 LOSE 徽标。"""
    won = result == "win"
    head_bg = ACCENT_BG if won else PANEL
    head_border = "rgba(196,112,78,0.25)" if won else BORDER_R
    card_border = "1.5px solid rgba(196,112,78,0.5)" if won else f"1px solid {BORDER}"
    name_color = ACCENT_D if won else INK
    badge = _badge(logo, team, 26, ACCENT if won else DARK2, CARD)
    if result == "win":
        tag = (
            f'<span style="font-size:13px;font-weight:800;letter-spacing:0.8px;'
            f'color:{WIN_C};margin-left:7px;">WIN</span>'
        )
    elif result == "lose":
        tag = (
            f'<span style="font-size:13px;font-weight:800;letter-spacing:0.8px;'
            f'color:{LOSE_C};margin-left:7px;">LOSE</span>'
        )
    else:
        tag = ""

    rows = ""
    for idx, p in enumerate(sorted(players, key=lambda x: x.rating, reverse=True)):
        top = "" if idx == 0 else f"border-top:1px solid {BORDER_S};"
        kd = p.kd
        kd_span = (
            f'<span style="font-size:14px;color:{MUTE};font-variant-numeric:tabular-nums;'
            f'letter-spacing:0.3px;min-width:52px;text-align:right;">{kd}</span>'
            if kd
            else '<span style="min-width:52px;"></span>'
        )
        rows += (
            f'<div style="display:flex;justify-content:space-between;align-items:center;padding:13px 20px;{top}">'
            f'<span style="display:flex;align-items:center;min-width:0;">'
            f'<span style="font-size:16px;color:{ROW};overflow:hidden;text-overflow:ellipsis;'
            f'white-space:nowrap;">{_esc(p.nick)}</span>'
            f"{_stars(majors.player_titles(p.nick), 11)}</span>"
            f'<div style="display:flex;align-items:center;gap:14px;">{kd_span}'
            f'<span style="font-size:17px;font-weight:600;color:{_rating_color(p.rating)};'
            f'font-variant-numeric:tabular-nums;min-width:38px;text-align:right;">{p.rating:.2f}</span></div></div>'
        )
    if not rows:
        rows = f'<div style="padding:15px 20px;color:{FAINT};font-size:15px;">评分待更新</div>'

    return (
        f'<div style="border:{card_border};border-radius:16px;overflow:hidden;background:{INNER};">'
        f'<div style="display:flex;align-items:center;gap:11px;padding:15px 20px;background:{head_bg};'
        f'border-bottom:1px solid {head_border};">{badge}'
        f'<span style="display:flex;align-items:center;min-width:0;">'
        f'<span style="font-size:18px;font-weight:600;color:{name_color};overflow:hidden;'
        f'text-overflow:ellipsis;white-space:nowrap;">{_esc(team)}</span>'
        f"{_stars(majors.team_titles(team), 12)}</span>{tag}</div>"
        f"<div>{rows}</div></div>"
    )


async def render_map_card(match: MatchDetail, map_index: int, when_text: str) -> bytes:
    return await _render_png(build_card_html(match, map_index, when_text))


# ———————————————————— 开赛卡(订阅的战队/选手比赛开始) ————————————————————
def _start_team_col(
    name: str, logo: Optional[str], rank: Optional[int],
    players: list[tuple[str, str]], align: str,
) -> str:
    badge = _badge(logo, name, 76, DARK, CARD)
    rank_html = (
        f'<div style="font-size:14px;color:{MUTE};font-weight:500;">世界第 {rank} 位</div>'
        if rank
        else ""
    )
    ai = "flex-start" if align == "left" else "flex-end"
    ta = "left" if align == "left" else "right"
    # 星标随列对齐镜像:左列星在名字右侧,右列星在名字左侧(始终朝向卡片中间)
    side = "right" if align == "left" else "left"

    def _nick_row(nk: str) -> str:
        st = _stars(majors.player_titles(nk), 10, side)
        txt = (
            f'<span style="font-size:16px;color:{SUB};letter-spacing:-0.1px;">{_esc(nk)}</span>'
        )
        inner = f"{txt}{st}" if side == "right" else f"{st}{txt}"
        return (
            f'<div style="display:flex;align-items:center;justify-content:{ai};'
            f'padding:3px 0;">{inner}</div>'
        )

    nicks = "".join(
        _nick_row(nk) for _pid, nk in players
    ) or f'<div style="font-size:14px;color:{FAINT};padding:3px 0;">阵容待定</div>'
    tstars = _stars(majors.team_titles(name), 14, side)
    tname = (
        f'<span style="font-size:26px;font-weight:700;color:{INK};letter-spacing:-0.3px;'
        f'line-height:1.1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'
        f'{_esc(name) or "待定"}</span>'
    )
    name_row = (
        f'<div style="display:flex;align-items:center;justify-content:{ai};max-width:100%;">'
        f'{tname}{tstars}</div>'
        if side == "right"
        else (
            f'<div style="display:flex;align-items:center;justify-content:{ai};max-width:100%;">'
            f"{tstars}{tname}</div>"
        )
    )
    return (
        f'<div style="display:flex;flex-direction:column;align-items:{ai};gap:9px;flex:1;'
        f'min-width:0;text-align:{ta};">{badge}{name_row}{rank_html}'
        f'<div style="margin-top:12px;display:flex;flex-direction:column;">{nicks}</div></div>'
    )


def build_match_start_html(
    match: MatchDetail,
    lineups: list[LineupTeam],
    when_text: str,
    start_text: str,
) -> str:
    by_ord = {t.ordinal: t for t in lineups}
    l1, l2 = by_ord.get(1), by_ord.get(2)
    p1 = l1.players if l1 else []
    p2 = l2.players if l2 else []
    r1 = l1.rank if l1 else None
    r2 = l2.rank if l2 else None

    parts = []
    if match.stage:
        parts.append(_esc(match.stage))
    parts.append(f"BO{match.best_of}")
    parts.append("LAN" if match.is_lan else "线上")
    subtitle = " · ".join(parts)
    event_badge = _badge(match.event_logo, match.event_name, 60, DARK, CARD)

    body = f"""
  <div style="display:flex;align-items:center;justify-content:space-between;gap:18px;">
   <div style="display:flex;align-items:center;gap:18px;min-width:0;">
    {event_badge}
    <div style="display:flex;flex-direction:column;gap:3px;min-width:0;">
     <div style="font-size:30px;font-weight:700;color:{INK};letter-spacing:-0.4px;line-height:1.1;">{_esc(match.event_name) or "CS2 比赛"}</div>
     <div style="font-size:17px;color:{SUB};font-weight:500;">{subtitle}</div>
    </div>
   </div>
   <div style="flex:none;background:{ACCENT_BG};color:{ACCENT_D};font-size:15px;font-weight:700;
        padding:8px 16px;border-radius:999px;letter-spacing:0.3px;">开赛提醒</div>
  </div>

  <div style="margin-top:30px;background:{PANEL};border:1px solid {BORDER};border-radius:18px;
       padding:30px 26px;display:flex;align-items:center;gap:16px;">
   {_start_team_col(match.team1, match.team1_logo, r1, p1, "left")}
   <div style="flex:none;display:flex;flex-direction:column;align-items:center;gap:8px;padding:0 4px;">
    <div style="font-size:40px;font-weight:800;color:{ACCENT};letter-spacing:1px;">VS</div>
    <div style="font-size:14px;color:{MUTE};white-space:nowrap;">{_esc(start_text)}</div>
   </div>
   {_start_team_col(match.team2, match.team2_logo, r2, p2, "right")}
  </div>

  {_vrs_panel(match)}

  <div style="display:flex;align-items:center;justify-content:space-between;margin-top:28px;
       padding-top:20px;border-top:1px solid {BORDER};">
   <div style="font-size:14px;color:{FAINT};">{_esc(when_text)}</div>
   <div style="font-size:14px;color:{MUTE};">HLTV · <span style="color:{ACCENT};font-weight:600;">hltv.org</span></div>
  </div>"""
    return _shell(body, "44px 48px 40px")


async def render_match_start_card(
    match: MatchDetail,
    lineups: list[LineupTeam],
    when_text: str,
    start_text: str,
) -> bytes:
    return await _render_png(build_match_start_html(match, lineups, when_text, start_text))


# ———————————————————— 赛事列表卡(/cs2 赛事) ————————————————————
def _fmt_range(start_ms: int, end_ms: int) -> str:
    """毫秒时间戳 → "7月21日 – 7月26日"(北京时间);跨年补年份。"""
    if not start_ms:
        return ""
    s = datetime.fromtimestamp(start_ms / 1000, CN_TZ)
    txt = f"{s.month}月{s.day}日"
    if s.year != datetime.now(CN_TZ).year:
        txt = f"{s.year}年" + txt
    if end_ms and end_ms != start_ms:
        e = datetime.fromtimestamp(end_ms / 1000, CN_TZ)
        txt += f" – {e.month}月{e.day}日"
    return txt


def build_events_html(events: list[EventDetail], when_text: str) -> str:
    rows = ""
    n = len(events)
    for i, e in enumerate(events):
        date = _fmt_range(e.start_unix, e.end_unix) or _esc(e.date_text)
        teams = f"{e.teams} 支队伍" if e.teams.isdigit() else _esc(e.teams)
        badge = _badge(e.logo, e.name, 56, ACCENT, CARD)
        bb = "" if i == n - 1 else f"border-bottom:1px solid {BORDER_R};"
        rows += (
            f'<div style="display:flex;align-items:center;gap:20px;padding:22px 4px;{bb}">'
            f"{badge}"
            f'<div style="flex:1;min-width:0;">'
            f'<div style="font-size:22px;font-weight:600;color:{INK};letter-spacing:-0.2px;">{_esc(e.name)}</div>'
            f'<div style="font-size:16px;color:{MUTE};margin-top:3px;">{_esc(e.location)}</div></div>'
            f'<div style="text-align:right;flex:none;">'
            f'<div style="font-size:20px;color:{INK};font-variant-numeric:tabular-nums;">{date}</div>'
            f'<div style="font-size:16px;color:{MUTE};margin-top:3px;">{teams}</div></div></div>'
        )

    body = f"""
  <div style="display:flex;align-items:flex-start;justify-content:space-between;">
   <div>
    <div style="font-size:27px;letter-spacing:-0.4px;color:{INK};"><span style="font-weight:700;">顶级赛事</span> <span style="font-weight:500;color:{SUB};">· 未来 3 个月</span></div>
    <div style="font-size:15px;color:{FAINT};margin-top:6px;">数据来源 HLTV · {_esc(when_text)}</div>
   </div>
   <div style="font-size:17px;color:{MUTE};font-weight:500;white-space:nowrap;padding-top:4px;">共 <span style="color:{INK};font-weight:600;">{n}</span> 项</div>
  </div>
  <div style="margin-top:22px;border-top:1px solid {LINE};">
{rows}
  </div>"""
    return _shell(body, "40px 48px 30px")


async def render_events_card(events: list[EventDetail], when_text: str) -> bytes:
    return await _render_png(build_events_html(events, when_text))


# ———————————————————— 今日赛程卡(/cs2 日程) ————————————————————
def _fmt_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, CN_TZ).strftime("%H:%M")


def _event_mark(logo: Optional[str], size: int = 24) -> str:
    """赛事分组头的小标记:有 logo 用 logo,否则赤陶圆角方块(菱形点)。"""
    img = _logo_plain(logo, size)
    if img:
        return (
            f'<span style="width:{size}px;height:{size}px;display:inline-flex;'
            f'align-items:center;justify-content:center;flex:none;">{img}</span>'
        )
    dot = round(size * 0.375)
    return (
        f'<span style="width:{size}px;height:{size}px;border-radius:7px;background:{ACCENT};'
        f"display:inline-flex;align-items:center;justify-content:center;flex:none;"
        f'box-shadow:0 1px 2px rgba(196,112,78,0.4);">'
        f'<span style="width:{dot}px;height:{dot}px;background:{CARD};border-radius:2px;'
        f'transform:rotate(45deg);"></span></span>'
    )


def _sched_logo(url: Optional[str], name: str = "", size: int = 22, dim: bool = False) -> str:
    """队标;有缓存 logo 用 logo,缺图则回退到深色首字母圆徽(与赛程/战报卡一致,不再留空白
    ——HLTV 图床受 Cloudflare 限流,队标字节常抓不到,留空看着像坏图)。无队名的占位(如
    未定对阵)仍用等大空占位,保证队标列竖直对齐。dim=已结束比赛的败者,置灰调浅。"""
    img = _logo_plain(url, size)
    if img:
        inner = (
            f'<span style="display:inline-flex;align-items:center;justify-content:center;'
            f'width:{size}px;height:{size}px;">{img}</span>'
        )
    elif name:
        inner = _circle(name, size, DARK2, CARD)
    else:
        return f'<span style="display:inline-block;width:{size}px;height:{size}px;"></span>'
    return _dim(inner) if dim else inner


def _sched_center(m: ScheduledMatch) -> str:
    """行中列:待开始 = "vs";进行中 = 目前大比分(赤陶)或 "vs";已结束 = 赛果,
    胜方绿 / 负方红(bo1 单场比分、bo3/5 大场比分,由 /results 天然给出)。"""
    if m.status == "finished" and m.score1 is not None and m.score2 is not None:
        c1 = GOOD if m.winner == "team1" else BAD
        c2 = GOOD if m.winner == "team2" else BAD
        return (
            f'<span style="font-size:18px;font-weight:700;font-variant-numeric:tabular-nums;'
            f'white-space:nowrap;"><span style="color:{c1};">{m.score1}</span>'
            f'<span style="color:{FAINT3};font-weight:500;padding:0 3px;">:</span>'
            f'<span style="color:{c2};">{m.score2}</span></span>'
        )
    if m.status == "live" and m.score1 is not None and m.score2 is not None:
        return (
            f'<span style="font-size:18px;font-weight:700;color:{ACCENT};'
            f'font-variant-numeric:tabular-nums;white-space:nowrap;">'
            f'{m.score1}<span style="font-weight:500;padding:0 3px;">:</span>{m.score2}</span>'
        )
    return f'<span style="font-size:13px;color:{FAINT};font-weight:500;">vs</span>'


def _sched_left(m: ScheduledMatch) -> str:
    """行左列:开始/结束时间;进行中显示 LIVE 徽标(卡上拿不到开赛时间)。"""
    if m.status == "live":
        return (
            f'<div style="display:inline-flex;align-items:center;gap:6px;">'
            f'<span style="width:7px;height:7px;border-radius:50%;background:{ACCENT};"></span>'
            f'<span style="font-size:14px;color:{ACCENT};font-weight:700;'
            f'letter-spacing:0.5px;">LIVE</span></div>'
        )
    color = FAINT if m.status == "finished" else ACCENT
    return (
        f'<div style="font-size:18px;color:{color};font-weight:500;'
        f'font-variant-numeric:tabular-nums;">{_fmt_time(m.start_unix)}</div>'
    )


def _sched_divider(status: str) -> str:
    """状态分割线:已结束 / 进行中 / 待开始 段落之间,细线夹一个小标签。"""
    label = {"finished": "已结束", "live": "进行中", "upcoming": "待开始"}.get(status, "")
    color = ACCENT if status == "live" else FAINT
    dot = (
        f'<span style="width:5px;height:5px;border-radius:50%;background:{ACCENT};"></span>'
        if status == "live"
        else ""
    )
    line = f'<span style="flex:1;height:1px;background:{LINE};"></span>'
    return (
        f'<div style="display:flex;align-items:center;gap:10px;padding:9px 4px;">'
        f'{line}{dot}<span style="font-size:12px;color:{color};font-weight:600;'
        f'letter-spacing:2px;">{label}</span>{line}</div>'
    )


def _sched_row(m: ScheduledMatch, last: bool) -> str:
    t1 = _esc(m.team1) if m.team1 else "待定"
    t2 = _esc(m.team2) if m.team2 else "待定"
    bo = m.best_of.upper() if m.best_of else ""
    right = {"finished": "已结束", "live": bo or "进行中"}.get(m.status, bo)
    right_color = ACCENT if m.status == "live" else FAINT
    name_color = SUB if m.status == "finished" else INK  # 已结束整行收敛,让比分红绿成为焦点
    bb = "" if last else f"border-bottom:1px solid {BORDER_R};"
    # 已结束比赛:败者队标置灰调浅(胜者正常);进行中/待开始两队均正常
    dim1 = m.status == "finished" and m.winner == "team2"
    dim2 = m.status == "finished" and m.winner == "team1"
    # 已结束:胜方一侧叠赤陶像素渐变(队名后最浓、朝外侧消散);外层容器 relative 承托叠层
    won = m.status == "finished" and m.winner in ("team1", "team2")
    wash = _win_wash_sched("left" if m.winner == "team1" else "right") if won else ""
    # 中间用固定列:队1名 | 队1标 | vs/比分 | 队2标 | 队2名 —— 队标/中列固定 → 竖直对齐
    return (
        f'<div style="position:relative;overflow:hidden;{bb}">{wash}'
        f'<div style="position:relative;z-index:1;display:grid;grid-template-columns:74px 1fr 54px;'
        f'align-items:center;padding:16px 4px;">'
        f"{_sched_left(m)}"
        f'<div style="display:grid;grid-template-columns:1fr 24px 64px 24px 1fr;align-items:center;gap:12px;">'
        f'<span style="display:flex;align-items:baseline;justify-content:flex-end;gap:9px;min-width:0;">'
        f"{_vrs_tag(m.team1, dim=dim1)}"
        f'<span style="min-width:0;font-size:18px;font-weight:500;color:{name_color};'
        f'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{t1}</span></span>'
        f'<span style="display:flex;justify-content:center;">{_sched_logo(m.team1_logo, m.team1, dim=dim1)}</span>'
        f'<span style="display:flex;justify-content:center;">{_sched_center(m)}</span>'
        f'<span style="display:flex;justify-content:center;">{_sched_logo(m.team2_logo, m.team2, dim=dim2)}</span>'
        f'<span style="display:flex;align-items:baseline;justify-content:flex-start;gap:9px;min-width:0;">'
        f'<span style="min-width:0;font-size:18px;font-weight:500;color:{name_color};'
        f'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{t2}</span>'
        f"{_vrs_tag(m.team2, dim=dim2)}</span></div>"
        f'<div style="text-align:right;font-size:14px;color:{right_color};font-weight:500;white-space:nowrap;">{right}</div></div></div>'
    )


def _recap_row(m: ScheduledMatch) -> str:
    """战报摘要的一行(紧凑版 _sched_row):时间 · 队标+队名 比分 队名+队标。

    空档期(上一比赛日已打完、下一比赛日还没开)时附在卡片顶部,让"昨晚打成啥样"
    和"接下来看什么"能一眼看全,又不跟主排期抢视觉重量——整行走 SUB/FAINT 弱化。
    """
    t1 = _esc(m.team1) if m.team1 else "待定"
    t2 = _esc(m.team2) if m.team2 else "待定"
    c1 = GOOD if m.winner == "team1" else BAD
    c2 = GOOD if m.winner == "team2" else BAD
    score = (
        f'<span style="color:{c1};">{m.score1}</span>'
        f'<span style="color:{FAINT3};font-weight:500;padding:0 2px;">:</span>'
        f'<span style="color:{c2};">{m.score2}</span>'
        if m.score1 is not None and m.score2 is not None
        else f'<span style="color:{FAINT};">–</span>'
    )
    # 与主排期的已结束行同款处理:胜方一侧叠赤陶像素渐变、败方队标置灰,一眼看出谁赢了
    # (紧凑行用 _win_wash_recap:同样的方向与消散,颗粒和浓度按行高缩了一档)。
    won = m.winner in ("team1", "team2")
    wash = _win_wash_recap("left" if m.winner == "team1" else "right") if won else ""
    return (
        f'<div style="position:relative;overflow:hidden;border-radius:10px;">{wash}'
        f'<div style="position:relative;z-index:1;display:grid;'
        f'grid-template-columns:56px 1fr 44px 1fr;align-items:center;gap:10px;padding:7px 2px;">'
        f'<span style="font-size:14px;color:{FAINT};font-variant-numeric:tabular-nums;">'
        f"{_fmt_time(m.start_unix)}</span>"
        f'<span style="display:flex;align-items:center;justify-content:flex-end;gap:7px;min-width:0;">'
        f'{_vrs_tag(m.team1, size=11, dim=m.winner == "team2")}'
        f'<span style="font-size:15px;color:{SUB};overflow:hidden;text-overflow:ellipsis;'
        f'white-space:nowrap;">{t1}</span>{_sched_logo(m.team1_logo, m.team1, size=18, dim=m.winner == "team2")}</span>'
        f'<span style="text-align:center;font-size:15px;font-weight:700;'
        f'font-variant-numeric:tabular-nums;white-space:nowrap;">{score}</span>'
        f'<span style="display:flex;align-items:center;gap:7px;min-width:0;">'
        f'{_sched_logo(m.team2_logo, m.team2, size=18, dim=m.winner == "team1")}'
        f'<span style="font-size:15px;color:{SUB};overflow:hidden;text-overflow:ellipsis;'
        f'white-space:nowrap;">{t2}</span>'
        f'{_vrs_tag(m.team2, size=11, dim=m.winner == "team1")}</span></div></div>'
    )


def _recap_block(recap: list[ScheduledMatch], label: str) -> str:
    """战报摘要区块:浅色底 + 标题行 + 若干紧凑赛果行。"""
    rows = "".join(_recap_row(m) for m in sorted(recap, key=lambda m: m.start_unix))
    return (
        f'<div style="margin-top:24px;padding:14px 18px 10px;border-radius:14px;'
        f'background:rgba(31,28,23,0.035);border:1px solid {BORDER_R};">'
        f'<div style="font-size:13px;color:{MUTE};font-weight:600;letter-spacing:1px;'
        f'padding-bottom:6px;">{_esc(label)}</div>{rows}</div>'
    )


def build_schedule_html(
    matches: list[ScheduledMatch],
    when_text: str,
    title: Optional[str] = None,
    subtitle: Optional[str] = None,
    recap: Optional[list[ScheduledMatch]] = None,
    recap_label: Optional[str] = None,
    vrs: Optional[dict[str, int]] = None,
) -> str:
    with _vrs_scope(vrs):
        return _build_schedule_html(matches, when_text, title, subtitle, recap, recap_label)


def _build_schedule_html(
    matches: list[ScheduledMatch],
    when_text: str,
    title: Optional[str],
    subtitle: Optional[str],
    recap: Optional[list[ScheduledMatch]],
    recap_label: Optional[str],
) -> str:
    # 组内排序:已结束 → 进行中 → 待开始,各按时间序(live 常无时间戳,靠状态位保序)
    rank = {"finished": 0, "live": 1, "upcoming": 2}
    matches = sorted(matches, key=lambda m: (rank.get(m.status, 2), m.start_unix))
    # 按赛事分组;/results 行没有 event_id,统一用赛事名归组,避免同一赛事分成两组
    groups: list[list] = []  # [event_name, event_logo, rows]
    index: dict[str, int] = {}
    for m in matches:
        key = (m.event_name or m.event_id or "").lower()
        if key not in index:
            index[key] = len(groups)
            groups.append([m.event_name, m.event_logo, []])
        g = groups[index[key]]
        g[1] = g[1] or m.event_logo  # 组内任一行有赛事 logo 就用上
        g[2].append(m)

    blocks = ""
    for gi, (ename, elogo, ms) in enumerate(groups):
        mt = "26px" if gi == 0 else "30px"
        rows = ""
        for j, m in enumerate(ms):
            nxt = ms[j + 1] if j + 1 < len(ms) else None
            cut = nxt is not None and nxt.status != m.status  # 状态切换处插分割线
            rows += _sched_row(m, last=(nxt is None or cut))
            if cut and nxt is not None:
                rows += _sched_divider(nxt.status)
        blocks += (
            f'<div style="display:flex;align-items:center;gap:11px;margin-top:{mt};">'
            f"{_event_mark(elogo)}"
            f'<span style="font-size:19px;font-weight:700;color:{INK};letter-spacing:-0.2px;">{_esc(ename)}</span></div>'
            f'<div style="margin-top:14px;border-top:1px solid {LINE};">{rows}</div>'
        )

    n_fin = sum(1 for m in matches if m.status == "finished")
    n_live = sum(1 for m in matches if m.status == "live")
    n_up = len(matches) - n_fin - n_live
    counts = []
    if n_fin:
        counts.append(f'已结束 <span style="color:{INK};font-weight:600;">{n_fin}</span>')
    if n_live:
        counts.append(f'进行中 <span style="color:{ACCENT};font-weight:600;">{n_live}</span>')
    counts.append(f'待开始 <span style="color:{INK};font-weight:600;">{n_up}</span>')
    today = datetime.now(CN_TZ)
    if title:
        head_main = f'<span style="font-weight:700;">{_esc(title)}</span>' + (
            f' <span style="font-weight:500;color:{SUB};">· {_esc(subtitle)}</span>'
            if subtitle
            else ""
        )
    else:
        head_main = (
            f'<span style="font-weight:700;">今日赛程</span> '
            f'<span style="font-weight:500;color:{SUB};">· {today.month}月{today.day}日</span>'
        )
    body = f"""
  <div style="display:flex;align-items:flex-start;justify-content:space-between;">
   <div>
    <div style="font-size:27px;letter-spacing:-0.4px;color:{INK};">{head_main}</div>
    <div style="font-size:15px;color:{FAINT};margin-top:6px;">关注赛事 · 数据来源 HLTV · {_esc(when_text)}</div>
   </div>
   <div style="font-size:17px;color:{MUTE};font-weight:500;white-space:nowrap;padding-top:4px;">{" · ".join(counts)} 场</div>
  </div>
{_recap_block(recap, recap_label) if recap and recap_label else ""}
{blocks}"""
    return _shell(body, "40px 48px 38px")


async def render_schedule_card(
    matches: list[ScheduledMatch],
    when_text: str,
    title: Optional[str] = None,
    subtitle: Optional[str] = None,
    recap: Optional[list[ScheduledMatch]] = None,
    recap_label: Optional[str] = None,
    vrs: Optional[dict[str, int]] = None,
) -> bytes:
    return await _render_png(
        build_schedule_html(matches, when_text, title, subtitle, recap, recap_label, vrs)
    )


# ═══════════════════ 赛程卡(/cs2 赛程 · 整届赛事结构:小组赛/瑞士轮 + 淘汰赛)═══════════════════
# 一张卡里按阶段呈现某届正在进行的赛事的完整赛程:瑞士轮(逐轮战绩列)、淘汰赛(对阵树)。
# 沿用暖米色审美;紧凑的对阵单元(bracket cell)与瑞士轮小卡是本卡新增的可复用组件。

_WEEKDAYS_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _fmt_dt(ms: int) -> str:
    """毫秒 → 北京时间的排期标注;不显示年份,显示 "M月D日 周X HH:MM"。"""
    if not ms:
        return "待定"
    d = datetime.fromtimestamp(ms / 1000, CN_TZ)
    return f"{d.month}月{d.day}日 {_WEEKDAYS_CN[d.weekday()]} {d.strftime('%H:%M')}"


def _slot_name(team: SlotTeam) -> str:
    return _esc(team.name) if team.known else (_esc(team.desc) if team.desc else "待定")


def _slot_badge(team: SlotTeam, size: int, dim: bool = False) -> str:
    """对阵占位徽标:确定用队标/首字母;未定用淡色空心占位,保持对齐。
    dim=已结束比赛的败者,置灰调浅。"""
    if team.known:
        badge = _badge(team.logo, team.name, size, DARK2, CARD)
        return _dim(badge) if dim else badge
    return (
        f'<span style="width:{size}px;height:{size}px;border-radius:50%;flex:none;'
        f'border:1.5px dashed {BORDER};display:inline-block;"></span>'
    )


# ———————————————————— 淘汰赛对阵单元 + 对阵树 ————————————————————
def _mu_foot(mu: Matchup, compact: bool = False, base_ms: int = 0) -> str:
    """对阵格脚注:时间 + BOx。

    compact=按比赛日分列时用,日期已在列头,格内只留时刻;但比赛日会跨午夜(欧洲赛事
    常打到次日 01:30),这种场次标上"次日",免得与列头日期打架。base_ms=该列的基准日。
    """
    bo = f"BO{mu.best_of}" if mu.best_of else ""
    if compact and not (mu.live or mu.finished):
        if mu.start_unix:
            d = datetime.fromtimestamp(mu.start_unix / 1000, CN_TZ)
            cross = (
                base_ms
                and d.date() != datetime.fromtimestamp(base_ms / 1000, CN_TZ).date()
            )
            prefix = "次日 " if cross else ""
            left = (
                f'<span style="color:{SUB};font-variant-numeric:tabular-nums;">'
                f"{prefix}{d:%H:%M}</span>"
            )
        else:
            left = f'<span style="color:{FAINT};">待定</span>'
        bo_html = f'<span style="color:{MUTE};">{bo}</span>' if bo else ""
        return (
            f'<div style="display:flex;align-items:center;justify-content:space-between;'
            f'margin-top:9px;font-size:12.5px;font-weight:500;">{left}{bo_html}</div>'
        )
    if mu.live:
        left = (
            f'<span style="display:inline-flex;align-items:center;gap:5px;">'
            f'<span style="width:6px;height:6px;border-radius:50%;background:{ACCENT};"></span>'
            f'<span style="color:{ACCENT};font-weight:700;letter-spacing:0.5px;">LIVE</span></span>'
        )
    elif mu.finished:
        left = f'<span style="color:{FAINT};">已结束</span>'
    else:
        left = f'<span style="color:{SUB};">{_fmt_dt(mu.start_unix)}</span>'
    bo_html = f'<span style="color:{MUTE};">{bo}</span>' if bo else ""
    return (
        f'<div style="display:flex;align-items:center;justify-content:space-between;'
        f'margin-top:9px;font-size:12.5px;font-weight:500;">{left}{bo_html}</div>'
    )


def _mu_team_row(
    team: SlotTeam,
    score: Optional[int],
    win: bool,
    lose: bool,
    size: int = 22,
    lead: bool = False,
) -> str:
    """lead=直播中暂时领先:只把比分点亮成赤陶,不动队名/不叠胜方渐变(还没赢)。"""
    name_col = INK if win else (SUB if lose else ROW)
    name_wt = "700" if win else "500"
    if not team.known:
        name_col, name_wt = FAINT2, "500"
    sc = ""
    if score is not None:
        sc_col = ACCENT if (win or lead) else (FAINT3 if lose else SUB)
        sc = (
            f'<span style="font-size:16px;font-weight:700;color:{sc_col};'
            f'font-variant-numeric:tabular-nums;min-width:16px;text-align:right;">{score}</span>'
        )
    return (
        f'<div style="display:flex;align-items:center;gap:9px;">'
        f"{_slot_badge(team, size, dim=lose)}"
        f'<span style="flex:1;min-width:0;font-size:15px;font-weight:{name_wt};color:{name_col};'
        f'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{_slot_name(team)}</span>'
        f'{_vrs_tag(team.name, size=11, dim=lose) if team.known else ""}{sc}</div>'
    )


def _bracket_team_row(
    team: SlotTeam, score: Optional[int], win: bool, lose: bool, lead: bool = False
) -> str:
    """淘汰赛对阵行;胜方整行叠一层赤陶像素渐变(从队标向比分消散)。"""
    row = _mu_team_row(team, score, win, lose, lead=lead)
    if not win:
        return row
    return (
        f'<div style="position:relative;overflow:hidden;border-radius:8px;">{_win_wash_row()}'
        f'<div style="position:relative;z-index:1;">{row}</div></div>'
    )


def _bracket_cell(mu: Matchup, compact: bool = False, base_ms: int = 0) -> str:
    w1 = mu.winner == "team1"
    w2 = mu.winner == "team2"
    accent = mu.live
    border = f"1.5px solid {ACCENT}" if accent else f"1px solid {BORDER}"
    bg = ACCENT_BG if accent else INNER
    # 直播中且已有大比分(BO3/BO5 打完了小场):领先方比分点亮,平手时两边都中性
    live_sc = mu.live and mu.score1 is not None and mu.score2 is not None
    l1 = bool(live_sc and mu.score1 > mu.score2)
    l2 = bool(live_sc and mu.score2 > mu.score1)
    return (
        f'<div style="border:{border};border-radius:13px;background:{bg};padding:12px 13px;">'
        f"{_bracket_team_row(mu.team1, mu.score1, w1, w2, l1)}"
        f'<div style="height:1px;background:{BORDER_S};margin:9px 0;"></div>'
        f"{_bracket_team_row(mu.team2, mu.score2, w2, w1, l2)}"
        f"{_mu_foot(mu, compact, base_ms)}</div>"
    )


def _round_col(rnd: BracketRound, extra_html: str = "") -> str:
    cells = "".join(_bracket_cell(m) for m in rnd.matchups)
    pos = "position:relative;" if extra_html else ""
    return (
        f'<div style="flex:1;min-width:186px;display:flex;flex-direction:column;">'
        f'<div style="font-size:13px;font-weight:700;color:{MUTE};letter-spacing:0.3px;'
        f'text-align:center;padding-bottom:12px;">{_esc(rnd.name)}</div>'
        f'<div style="flex:1;{pos}display:flex;flex-direction:column;justify-content:space-around;gap:14px;">'
        f"{cells}{extra_html}</div></div>"
    )


def _third_place_block(third: BracketRound) -> str:
    """季军赛小块:放进总决赛所在列的下半区留白——一个小标题 + 一个对阵框,不再单独成节。"""
    cells = "".join(_bracket_cell(m) for m in third.matchups)
    return (
        f"<div>"
        f'<div style="font-size:13px;font-weight:700;color:{MUTE};letter-spacing:0.3px;'
        f'text-align:center;padding-bottom:12px;">季军赛</div>'
        f"{cells}</div>"
    )


def _day_head(ms: int) -> str:
    d = datetime.fromtimestamp(ms / 1000, CN_TZ)
    return f"{d.month}月{d.day}日 {_WEEKDAYS_CN[d.weekday()]}"


def _round_day_columns(rnd: BracketRound) -> Optional[str]:
    """单轮多场 → **按比赛日分列**:一天一列,列头标日期,格内只留开赛时刻。

    「胜者挑对手」的赛制(BLAST Bounty)只公布下一轮,于是常出现"只有一轮已知、这一轮
    16 场、摊在 4 个晚上"的情形。按比赛日分列比无脑网格直观得多——一眼看出"周二这
    4 场、周三那 4 场";列数不合适(1 列或多于 5 列会挤)时返回 None,交给网格兜底。
    比赛日的定义与 /cs2 日程 共用 ``hltv.cluster_match_days``。
    """
    days = cluster_match_days(rnd.matchups)
    rest = [m for m in rnd.matchups if not m.start_unix]
    n = len(days) + (1 if rest else 0)
    if not 2 <= n <= 5:
        return None
    groups = [(_day_head(d[0].start_unix), d[0].start_unix, d) for d in days]
    if rest:
        groups.append(("待定", 0, rest))
    cols = ""
    for head, base, ms in groups:
        cells = "".join(_bracket_cell(m, compact=True, base_ms=base) for m in ms)
        cols += (
            f'<div style="flex:1;min-width:0;display:flex;flex-direction:column;">'
            f'<div style="font-size:12.5px;font-weight:600;color:{SUB};text-align:center;'
            f'padding-bottom:10px;border-bottom:1px solid {BORDER_S};margin-bottom:12px;'
            f'white-space:nowrap;">{_esc(head)}</div>'
            f'<div style="display:flex;flex-direction:column;gap:12px;">{cells}</div></div>'
        )
    return (
        f'<div style="flex:1;display:flex;flex-direction:column;">'
        f'<div style="font-size:13px;font-weight:700;color:{MUTE};letter-spacing:0.3px;'
        f'text-align:center;padding-bottom:14px;">{_esc(rnd.name)}</div>'
        f'<div style="display:flex;gap:22px;align-items:flex-start;">{cols}</div></div>'
    )


def _round_grid(rnd: BracketRound) -> str:
    """单轮多场的兜底布局:等宽网格(比赛日分不出合适列数时用)。"""
    cols = 3 if len(rnd.matchups) >= 9 else 2
    cells = "".join(_bracket_cell(m) for m in rnd.matchups)
    return (
        f'<div style="flex:1;display:flex;flex-direction:column;">'
        f'<div style="font-size:13px;font-weight:700;color:{MUTE};letter-spacing:0.3px;'
        f'text-align:center;padding-bottom:12px;">{_esc(rnd.name)}</div>'
        f'<div style="display:grid;grid-template-columns:repeat({cols},1fr);gap:12px 16px;">'
        f"{cells}</div></div>"
    )


# 单轮超过这么多场就不再堆成一根长柱(优先按比赛日分列,分不出来才用等宽网格)
_ROUND_GRID_MIN = 6


def _bracket_row(
    rounds: list[BracketRound], tier: str = "", third: Optional[BracketRound] = None
) -> str:
    if not rounds:
        return ""
    third_html = _third_place_block(third) if third else ""
    if len(rounds) == 1 and len(rounds[0].matchups) >= _ROUND_GRID_MIN:
        cols = _round_day_columns(rounds[0]) or _round_grid(rounds[0])
        if third:  # 网格布局没有"末列",季军赛退化成追加的一列
            cols += f'<div style="flex:none;min-width:186px;">{third_html}</div>'
    else:
        if third and len(rounds) >= 3:
            # 树够深时末列(总决赛)下方有大片留白:季军赛绝对定位在列高 3/4 处,
            # 纵向与下半区半决赛齐平;总决赛本体不参与分配,仍居中——对称构图不破坏。
            third_html = (
                '<div style="position:absolute;top:75%;left:0;right:0;'
                f'transform:translateY(-50%);">{third_html}</div>'
            )
        # 浅树(≤2 列)没有那片留白,季军赛作为末列第二个 flex 项由 space-around 均布
        cols = "".join(
            _round_col(r, third_html if r is rounds[-1] else "") for r in rounds
        )
    tier_html = (
        f'<div style="font-size:13px;font-weight:600;color:{ACCENT_D};'
        f'letter-spacing:1px;margin:2px 0 14px 2px;">{_esc(tier)}</div>'
        if tier
        else ""
    )
    return f'{tier_html}<div style="display:flex;gap:16px;align-items:stretch;">{cols}</div>'


def _bracket_summary(rounds: list[BracketRound], lead: str) -> str:
    """赛制概要:尚未产生对阵的轮次用一行 round chips 交代,代替整片"待定 vs 待定"。"""
    chips = ""
    for r in rounds:
        bo_vals = {mu.best_of for mu in r.matchups if mu.best_of}
        bo = f"BO{bo_vals.pop()}" if len(bo_vals) == 1 else ""
        chips += (
            f'<div style="display:inline-flex;align-items:center;gap:8px;background:{INNER};'
            f'border:1px solid {BORDER};border-radius:11px;padding:9px 15px;">'
            f'<span style="font-size:15px;font-weight:600;color:{INK};">{_esc(r.name)}</span>'
            + (
                f'<span style="font-size:12px;font-weight:600;color:{MUTE};">{bo}</span>'
                if bo
                else ""
            )
            + "</div>"
        )
    lead_html = (
        f'<div style="font-size:14px;color:{SUB};margin-bottom:14px;">{_esc(lead)}</div>'
        if lead
        else ""
    )
    return (
        f'<div style="margin-top:18px;">'
        f"{lead_html}"
        f'<div style="display:flex;flex-wrap:wrap;gap:10px;">{chips}</div></div>'
    )


# HLTV 会把季军赛单独挂成一个 bracket placeholder(如 EWC 的 "3rd Place Decider Match"),
# 名字带这些关键词、且整棵树只有一场 —— 这类树不值得单独一节,并入主淘汰赛渲染。
_THIRD_PLACE_RE = re.compile(r"3rd\s*place|third\s*place|consolidation|bronze", re.I)


def _third_place_round(b: Bracket) -> Optional[BracketRound]:
    """若 b 是"独立成树的季军赛"则返回其唯一轮次(名字统一成「季军赛」),否则 None。"""
    rounds = [r for r in b.all_rounds() if r.matchups]
    if len(rounds) != 1 or len(rounds[0].matchups) != 1:
        return None
    text = f"{b.name} {rounds[0].name}"
    if not (_THIRD_PLACE_RE.search(text) or "季军" in text):
        return None
    rounds[0].name = "季军赛"
    return rounds[0]


def _adopt_third_place(
    brackets: list[Bracket],
) -> tuple[list[Bracket], dict[int, BracketRound]]:
    """把独立成树的季军赛摘出来挂到主淘汰赛树上。

    返回 (去掉季军赛树后的列表, {id(宿主树): 季军赛轮})。宿主取页面顺序上它前面最近的
    一棵主树(HLTV 把季军赛紧跟主淘汰赛之后挂),排最前则取其后第一棵;没有可挂靠的
    主树、或宿主已挂过一场(理论上不会发生)时保持原样,仍单独成节兜底。
    """
    third_at: dict[int, BracketRound] = {}
    for i, b in enumerate(brackets):
        r = _third_place_round(b)
        if r is not None:
            third_at[i] = r
    if not third_at or len(third_at) == len(brackets):
        return brackets, {}
    kept_all = [(i, b) for i, b in enumerate(brackets) if i not in third_at]
    kept = [b for _, b in kept_all]
    # 宿主只在主淘汰赛树里选:季军赛挂到小组面板上没有意义
    hosts = [(i, b) for i, b in kept_all if not _is_group_bracket(b)] or kept_all
    adopted: dict[int, BracketRound] = {}
    for i, r in third_at.items():
        prev = [b for j, b in hosts if j < i]
        host = prev[-1] if prev else hosts[0][1]
        if id(host) in adopted:
            kept.append(brackets[i])  # 宿主已有一场季军赛:极罕见,保底单独成节
            continue
        adopted[id(host)] = r
    return kept, adopted


def _is_group_bracket(b: Bracket) -> bool:
    """小组赛对阵树(Group A/B/… 或含「小组」):版式上与主淘汰赛分开,进小组网格。"""
    name = (b.name or "").strip()
    return bool(re.match(r"group\b", name, re.I)) or "小组" in name


def _bracket_tree(
    b: Bracket, third_live: Optional[BracketRound] = None, group_like: bool = False
) -> str:
    """一棵对阵树的树体(不含分节标题 / chips 尾巴),主淘汰赛节与小组面板共用。

    ``group_like``:小组树只画真实产生过对阵的轮次——HLTV 的固定树里常带一串
    **实际不会打**的收尾轮次(胜者组决赛/季军赛/总决赛等,小组只打到出线为止),
    这些轮次要么全待定、要么被 /matches 排期回填错挂上主赛段的时间,展示纯属冗余。
    """

    def known(rounds: list[BracketRound]) -> list[BracketRound]:
        rs = [r for r in rounds if not r.is_pending()]
        if group_like:
            rs = [
                r
                for r in rs
                if any(
                    mu.finished or mu.live or (mu.team1.known and mu.team2.known)
                    for mu in r.matchups
                )
            ]
        return rs

    if b.kind == "single":
        return _bracket_row(known(b.upper), third=third_live)
    # 季军赛挂到最后一个非空分组(通常是决赛阶段)的末列下
    tiers = [
        (known(b.upper), "胜者组 Upper Bracket"),
        (known(b.lower), "败者组 Lower Bracket"),
        (known(b.finals), "决赛阶段 Finals"),
    ]
    last_idx = max((i for i, (rs, _) in enumerate(tiers) if rs), default=-1)
    parts = []
    for i, (rounds, tier) in enumerate(tiers):
        if rounds:
            parts.append(_bracket_row(rounds, tier, third=third_live if i == last_idx else None))
    return '<div style="display:flex;flex-direction:column;gap:26px;">' + "".join(parts) + "</div>"


def _bracket_section(b: Bracket, third: Optional[BracketRound] = None) -> str:
    """主淘汰赛树一节:已产生对阵的轮次画成树,尚未产生的轮次收成一行赛制 chips。

    BLAST Bounty 这类「胜者挑对手」的赛制只公布下一轮,后面几轮长期是空的 —— 把空轮次
    铺成"待定 vs 待定"既占版面又没信息,故拆成"已知树 + 后续轮次摘要"两截。

    ``third``:HLTV 单独挂树的季军赛(见 ``_adopt_third_place``),并入本节渲染——
    已排期的画在总决赛同列下方,尚未产生的收进末尾的赛制 chips。
    小组树不走这里,见 ``_group_stage_section``。
    """
    kind_cn = "单败淘汰" if b.kind == "single" else "双败淘汰"
    caption = " · ".join(x for x in (b.name, kind_cn) if x) or kind_cn
    if b.is_pending():
        extra = [third] if third else []
        return _section("淘汰赛", caption) + _bracket_summary(
            b.all_rounds() + extra, "对阵将在前一阶段结束后产生,当前赛制:"
        )
    third_live = third if third is not None and not third.is_pending() else None
    body = _bracket_tree(b, third_live)
    rest = b.pending_rounds()
    if third is not None and third_live is None:
        rest = rest + [third]
    tail = (
        _bracket_summary(rest, "")
        if rest
        else ""
    )
    return (
        _section("淘汰赛", caption)
        + f'<div style="margin-top:18px;">{body}</div>'
        + tail
    )


def _group_panel(b: Bracket) -> str:
    """单个小组的面板:米灰底 + 组名标题条,内部是过滤后的小组对阵树。"""
    if b.is_pending():
        body = _bracket_summary(b.all_rounds(), "对阵将在小组抽签/前一阶段后产生:")
    else:
        body = _bracket_tree(b, group_like=True)
    kind_cn = "单败淘汰" if b.kind == "single" else "双败淘汰"
    return (
        f'<div style="background:{PANEL};border:1px solid {BORDER};border-radius:18px;'
        f'padding:20px 22px 24px;">'
        f'<div style="display:flex;align-items:baseline;gap:10px;padding-bottom:13px;'
        f'border-bottom:1px solid {LINE};margin-bottom:18px;">'
        f'<span style="font-size:19px;font-weight:700;color:{INK};letter-spacing:-0.2px;">'
        f"{_esc(b.name)}</span>"
        f'<span style="font-size:12.5px;font-weight:600;color:{MUTE};">{kind_cn}</span></div>'
        f"{body}</div>"
    )


def _group_stage_section(groups: list[Bracket]) -> str:
    """小组赛总节:2 组左右并列、3-4 组 2×2 网格(更多组继续两列往下排),
    每组一个独立面板,组间界限一目了然;只有 1 组时面板独占整行。"""
    kinds = {g.kind for g in groups}
    kind_cn = (
        "双败淘汰" if kinds == {"double"} else "单败淘汰" if kinds == {"single"} else ""
    )
    caption = " · ".join(x for x in (f"{len(groups)} 个小组", kind_cn) if x)
    panels = "".join(_group_panel(g) for g in groups)
    if len(groups) == 1:
        grid = panels
    else:
        grid = (
            f'<div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));'
            f'gap:20px;align-items:start;">{panels}</div>'
        )
    return _section("小组赛", caption) + f'<div style="margin-top:20px;">{grid}</div>'


# ———————————————————— 瑞士轮 ————————————————————
def _swiss_score(mu: Matchup) -> str:
    """瑞士轮小卡中列:已结束=胜负比分(胜方赤陶),直播=目前大比分 + LIVE,未开始=时间/vs。"""
    if mu.finished and mu.score1 is not None:
        c1 = ACCENT if mu.winner == "team1" else FAINT3
        c2 = ACCENT if mu.winner == "team2" else FAINT3
        return (
            f'<span style="font-size:14px;font-weight:700;font-variant-numeric:tabular-nums;'
            f'white-space:nowrap;"><span style="color:{c1};">{mu.score1}</span>'
            f'<span style="color:{FAINT3};font-weight:500;">:</span>'
            f'<span style="color:{c2};">{mu.score2}</span></span>'
        )
    if mu.live:
        live_tag = (
            f'<span style="font-size:11px;font-weight:700;color:{ACCENT};'
            f'letter-spacing:0.5px;">LIVE</span>'
        )
        if mu.score1 is None or mu.score2 is None:
            return live_tag
        # 大比分已有(BO3/BO5 打完了小场)→ 比分为主、LIVE 收成脚标,别只写"进行中"
        c1 = ACCENT if mu.score1 > mu.score2 else FAINT3
        c2 = ACCENT if mu.score2 > mu.score1 else FAINT3
        return (
            f'<span style="display:inline-flex;flex-direction:column;align-items:center;gap:1px;">'
            f'<span style="font-size:14px;font-weight:700;font-variant-numeric:tabular-nums;'
            f'white-space:nowrap;"><span style="color:{c1};">{mu.score1}</span>'
            f'<span style="color:{FAINT3};font-weight:500;">:</span>'
            f'<span style="color:{c2};">{mu.score2}</span></span>'
            f'<span style="font-size:9px;font-weight:700;color:{ACCENT};'
            f'letter-spacing:0.5px;opacity:0.72;">LIVE</span></span>'
        )
    return f'<span style="font-size:12px;color:{FAINT};font-weight:500;">vs</span>'


def _swiss_mu(mu: Matchup) -> str:
    """瑞士轮一场:两队标 + 中间比分/vs(仿 HLTV 只用队标不占名字,保持窄列)。
    已结束的对阵:败者队标置灰调浅。"""
    b1 = _slot_badge(mu.team1, 26, dim=(mu.finished and mu.winner == "team2"))
    b2 = _slot_badge(mu.team2, 26, dim=(mu.finished and mu.winner == "team1"))
    live_bd = f"1.5px solid {ACCENT}" if mu.live else f"1px solid {BORDER}"
    bg = ACCENT_BG if mu.live else INNER
    # 已结束:胜方队标一侧叠赤陶像素渐变(外缘最浓、向中间消散);容器 relative 承托叠层
    won = mu.finished and mu.winner in ("team1", "team2")
    wash = _win_wash_swiss("left" if mu.winner == "team1" else "right") if won else ""
    return (
        f'<div style="position:relative;overflow:hidden;border:{live_bd};border-radius:11px;'
        f'background:{bg};">{wash}'
        f'<div style="position:relative;z-index:1;padding:8px 10px;'
        f'display:grid;grid-template-columns:26px 1fr 26px;align-items:center;gap:8px;">'
        f'{b1}<span style="display:flex;justify-content:center;">{_swiss_score(mu)}</span>{b2}</div></div>'
    )


def _swiss_outcome_band(cell) -> str:
    """晋级 / 淘汰的结果带:绿 / 红底色块 + 「已晋级 3:0」标签 + 队标行。
    仿 HLTV——把已定生死的队伍单独成带(绿顶红底),不与本轮对阵混为一谈。"""
    adv = cell.kind == "advanced"
    color, bg, bd = (GOOD, GOOD_BG, GOOD_BD) if adv else (BAD, BAD_BG, BAD_BD)
    label = "已晋级" if adv else "已淘汰"
    logos = "".join(
        f'<span style="width:26px;height:26px;display:inline-flex;align-items:center;'
        f'justify-content:center;flex:none;">'
        f"{_logo_plain(t.logo, 26) or _circle(t.name, 26, DARK2, CARD)}</span>"
        for t in cell.teams
    )
    rec = (
        f'<span style="margin-left:auto;font-size:13px;font-weight:700;color:{color};'
        f'font-variant-numeric:tabular-nums;letter-spacing:0.5px;">{_esc(cell.record)}</span>'
        if cell.record
        else ""
    )
    return (
        f'<div style="border:1px solid {bd};border-radius:12px;background:{bg};padding:10px 12px;">'
        f'<div style="display:flex;align-items:center;gap:7px;margin-bottom:9px;">'
        f'<span style="width:6px;height:6px;border-radius:50%;background:{color};flex:none;"></span>'
        f'<span style="font-size:12px;font-weight:700;color:{color};letter-spacing:1.5px;">{label}</span>'
        f"{rec}</div>"
        f'<div style="display:flex;flex-wrap:wrap;gap:8px;">{logos}</div></div>'
    )


def _swiss_record_pill(rec: str) -> str:
    """战绩池顶部的居中胶囊(如 2:0 / 2:1)。"""
    return (
        f'<div style="text-align:center;margin-bottom:12px;">'
        f'<span style="display:inline-block;background:{PANEL};border-radius:10px;'
        f"padding:5px 17px;font-size:17px;font-weight:700;color:{SUB};"
        f'font-variant-numeric:tabular-nums;letter-spacing:1.5px;">{_esc(rec)}</span></div>'
        if rec
        else ""
    )


def _swiss_pool(cell) -> str:
    """已抽签的战绩池(如 2:0):居中战绩胶囊 + 该池对阵卡(已排期/进行/已完赛)。"""
    inner = "".join(_swiss_mu(m) for m in cell.matchups)
    return (
        f'<div style="display:flex;flex-direction:column;">{_swiss_record_pill(cell.record)}'
        f'<div style="display:flex;flex-direction:column;gap:8px;">{inner}</div></div>'
    )


def _swiss_pool_cluster(cell) -> str:
    """未抽签的下一轮分组池(如 2:1):只展示池中队伍,不配对——配对要等本轮打完才产生。
    未落位的名额用虚线占位,提示该池还会有队伍加入(仿 HLTV「TBD 分组池」)。"""
    badges = "".join(_slot_badge(t, 28) for t in cell.teams)
    box = (
        f'<div style="border:1px dashed {BORDER};border-radius:12px;background:{INNER2};'
        f'padding:11px 12px;">'
        f'<div style="font-size:11px;font-weight:600;color:{MUTE};letter-spacing:1px;'
        f'margin-bottom:9px;">对阵待定</div>'
        f'<div style="display:flex;flex-wrap:wrap;gap:8px;">{badges}</div></div>'
    )
    return (
        f'<div style="display:flex;flex-direction:column;">{_swiss_record_pill(cell.record)}'
        f"{box}</div>"
    )


def _swiss_group_divider() -> str:
    """同一轮里不同战绩池(如 2:1 / 1:2)之间的分割线,让分组更直观。"""
    return f'<div style="height:1px;background:{LINE};margin:18px 4px;flex:none;"></div>'


def _cell_has_known(cell) -> bool:
    """该战绩块是否含已确定的队伍(晋级/淘汰带、分组池成员里有确定队;或对阵里有确定队)。
    注意分组池成员含占位(未知),故须看 `t.known` 而非「是否有 teams」。"""
    if any(t.known for t in cell.teams):
        return True
    return any(m.team1.known or m.team2.known or m.finished or m.live for m in cell.matchups)


def _swiss_section(sw: SwissStage) -> str:
    cols = ""
    for i, col in enumerate(sw.columns):
        # 整列都还没有任何确定的队伍(如更后面的全 TBD 轮)→ 跳过,保持卡片干净
        if not any(_cell_has_known(c) for c in col.cells):
            continue
        adv = [c for c in col.cells if c.kind == "advanced" and c.teams]
        elim = [c for c in col.cells if c.kind == "eliminated" and c.teams]
        pools = [c for c in col.cells if c.kind == "normal" and (c.matchups or c.teams)]

        adv_html = "".join(_swiss_outcome_band(c) for c in adv)
        elim_html = "".join(_swiss_outcome_band(c) for c in elim)
        pool_html = ""
        for pi, c in enumerate(pools):
            if pi:
                pool_html += _swiss_group_divider()
            # 已抽签→对阵卡;未抽签→只列池成员(不配对)
            pool_html += _swiss_pool(c) if c.matchups else _swiss_pool_cluster(c)
        # 保险:即便列被判为「有内容」,若三种渲染块都为空也跳过,绝不留孤零零的轮次标题
        if not (adv_html or elim_html or pool_html):
            continue

        # 列内竖向布局:晋级带顶、对阵池居中、淘汰带底 —— 复刻 HLTV 的「绿升红沉」。
        adv_block = f'<div style="margin-bottom:16px;">{adv_html}</div>' if adv_html else ""
        elim_block = f'<div style="margin-top:16px;">{elim_html}</div>' if elim_html else ""
        middle = (
            f'<div style="flex:1;display:flex;flex-direction:column;">{adv_block}'
            f'<div style="flex:1;display:flex;flex-direction:column;justify-content:center;">'
            f"{pool_html}</div>{elim_block}</div>"
        )

        if col.status == "active":
            head_col, head_txt = ACCENT_D, f"第 {i + 1} 轮 · 进行中"
        elif col.status == "finished":
            head_col, head_txt = MUTE, f"第 {i + 1} 轮"
        else:
            head_col, head_txt = FAINT2, f"第 {i + 1} 轮"
        cols += (
            f'<div style="flex:1;min-width:0;display:flex;flex-direction:column;">'
            f'<div style="font-size:12.5px;font-weight:700;color:{head_col};text-align:center;'
            f'letter-spacing:0.3px;padding-bottom:16px;">{head_txt}</div>'
            f"{middle}</div>"
        )
    if not cols:  # 全部轮次都还没确定队伍(赛事未开打)→ 不铺空节
        return ""
    # align-items:stretch → 各列等高;晋级带顶/淘汰带底靠内层 flex 撑开,呈对角分布
    return (
        _section("小组赛", "瑞士轮 Swiss Stage")
        + f'<div style="margin-top:18px;display:flex;gap:14px;align-items:stretch;">{cols}</div>'
    )


# ———————————————————— 循环赛积分表(兜底)————————————————————
def _group_section(groups: list[GroupStanding]) -> str:
    blocks = ""
    for g in groups:
        rows = ""
        for r in g.rows:
            badge = _badge(r.get("logo"), r.get("name", ""), 24, DARK2, CARD)
            vals = "".join(
                f'<span style="font-size:14px;color:{SUB};font-variant-numeric:tabular-nums;'
                f'min-width:34px;text-align:center;">{_esc(v)}</span>'
                for v in r.get("cells", [])[:5]
            )
            rows += (
                f'<div style="display:flex;align-items:center;gap:11px;padding:11px 4px;'
                f'border-top:1px solid {BORDER_S};">'
                f'<span style="font-size:14px;color:{MUTE};width:20px;">{_esc(str(r.get("rank", "")))}</span>'
                f'{badge}<span style="flex:1;min-width:0;font-size:15px;color:{INK};font-weight:500;'
                f'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{_esc(r.get("name", ""))}</span>'
                f'{_vrs_tag(r.get("name", ""), size=11)}{vals}</div>'
            )
        blocks += (
            f'<div style="flex:1;min-width:300px;background:{INNER};border:1px solid {BORDER};'
            f'border-radius:16px;padding:8px 16px 14px;">'
            f'<div style="font-size:16px;font-weight:700;color:{INK};padding:10px 4px 4px;">{_esc(g.name)}</div>'
            f"{rows}</div>"
        )
    return (
        _section("小组赛", "Groups")
        + f'<div style="margin-top:18px;display:flex;flex-wrap:wrap;gap:16px;">{blocks}</div>'
    )


# ———————————————————— 组合成卡 ————————————————————
def _section(cn: str, en: str) -> str:
    return (
        f'<div style="display:flex;align-items:baseline;gap:12px;margin-top:36px;'
        f'padding-bottom:14px;border-bottom:1px solid {LINE};">'
        f'<span style="font-size:23px;font-weight:700;color:{INK};letter-spacing:-0.3px;">{_esc(cn)}</span>'
        f'<span style="font-size:14px;font-weight:500;color:{MUTE};letter-spacing:0.3px;">{_esc(en)}</span></div>'
    )


def _status_pill(status: str) -> str:
    s = (status or "").strip()
    if s.lower() == "live":
        return (
            f'<span style="display:inline-flex;align-items:center;gap:6px;background:{ACCENT_BG};'
            f'border-radius:999px;padding:5px 13px;">'
            f'<span style="width:7px;height:7px;border-radius:50%;background:{ACCENT};"></span>'
            f'<span style="font-size:13px;font-weight:700;color:{ACCENT_D};letter-spacing:0.5px;">进行中</span></span>'
        )
    cn = {"upcoming": "即将开始", "concluded": "已结束", "ongoing": "进行中"}.get(s.lower(), s)
    if not cn:
        return ""
    return (
        f'<span style="display:inline-flex;align-items:center;background:{PANEL};'
        f'border-radius:999px;padding:5px 13px;font-size:13px;font-weight:600;color:{SUB};">{_esc(cn)}</span>'
    )


def build_event_schedule_html(
    sched: EventSchedule, when_text: str, vrs: Optional[dict[str, int]] = None
) -> str:
    with _vrs_scope(vrs):
        return _build_event_schedule_html(sched, when_text)


def _build_event_schedule_html(sched: EventSchedule, when_text: str) -> str:
    badge = _badge(sched.logo, sched.name, 62, DARK, CARD)
    meta_bits = [b for b in (sched.date_text, sched.location) if b]
    meta = " · ".join(_esc(b) for b in meta_bits)
    prize = (
        f'<div style="font-size:16px;color:{INK};font-weight:600;'
        f'font-variant-numeric:tabular-nums;">{_esc(sched.prize)}</div>'
        if sched.prize
        else ""
    )

    swiss_html = _swiss_section(sched.swiss) if sched.swiss else ""
    group_html = _group_section(sched.groups) if sched.groups else ""
    # 独立成树的季军赛并入主淘汰赛节渲染(总决赛下方一框),不再单独占一节
    brackets, thirds = _adopt_third_place(sched.brackets)
    # 小组树(Group A/B/…)与主淘汰赛分开:小组进网格节(2 组并列 / 4 组 2×2)
    group_br = [b for b in brackets if _is_group_bracket(b)]
    main_br = [b for b in brackets if not _is_group_bracket(b)]
    groups_sec = _group_stage_section(group_br) if group_br else ""
    # 已排定对阵的阶段在上、空架阶段在下(赛事页可能同时挂 Stage 1/Stage 2 两棵树)
    live_br = [b for b in main_br if not b.is_pending()]
    pending_br = [b for b in main_br if b.is_pending()]
    # 淘汰赛对阵已排定(有确定队伍/已开打)时置于小组赛之上——此时观众更关心淘汰赛走势;
    # 仍是空架(全 TBD)时垫在末尾,只作赛制概要。
    sections = (
        "".join(_bracket_section(b, thirds.get(id(b))) for b in live_br)
        + groups_sec
        + swiss_html
        + group_html
        + "".join(_bracket_section(b, thirds.get(id(b))) for b in pending_br)
    )
    if not sections:
        sections = (
            f'<div style="margin-top:40px;text-align:center;font-size:16px;color:{MUTE};'
            f'padding:30px;">暂无可展示的赛程结构</div>'
        )

    body = f"""
  <div style="display:flex;align-items:center;gap:18px;">
   {badge}
   <div style="flex:1;min-width:0;display:flex;flex-direction:column;gap:5px;">
    <div style="font-size:30px;font-weight:700;color:{INK};letter-spacing:-0.4px;line-height:1.1;">{_esc(sched.name) or "赛程"}</div>
    <div style="font-size:15px;color:{SUB};font-weight:500;">{meta}</div>
   </div>
   <div style="display:flex;flex-direction:column;align-items:flex-end;gap:7px;flex:none;">
    {_status_pill(sched.status)}
    {prize}
   </div>
  </div>
{sections}
  <div style="display:flex;align-items:center;justify-content:space-between;margin-top:34px;
       padding-top:20px;border-top:1px solid {BORDER};">
   <div style="font-size:14px;color:{FAINT};">{_esc(when_text)}</div>
   <div style="font-size:14px;color:{MUTE};">HLTV · <span style="color:{ACCENT};font-weight:600;">hltv.org</span></div>
  </div>"""
    return _shell(body, "44px 48px 40px")


async def render_event_schedule_card(
    sched: EventSchedule, when_text: str, vrs: Optional[dict[str, int]] = None
) -> bytes:
    return await _render_png(build_event_schedule_html(sched, when_text, vrs))


# ═══════════════════ 帮助卡(/cs2 功能菜单)═══════════════════
# 发 /cs2(无子命令)时用图片展示支持的功能,而非一段文字。两版:
# 公开版(查询 + 订阅)对所有群;管理版(多一节「管理·调试」)只在调试群/超管私聊出现。

# HLTV 的品牌标识:蓝底方块里的「流星 / 彗星」——一道扫向右上的白色曲刃,左下收成
# 卷钩状的彗头,外侧一圈由大到小的尾迹圆点。下面这条路径是照 HLTV 官方 logo(TopLogo2x
# 的方形图标)逐像素描摹、化简、平滑得来的矢量(0–24 viewBox),再按卡片配色改成
# 白色描在赤陶方块上(而非直接贴原图),与三张战报卡的暖米色主题统一。
_HLTV_SWOOSH = (
    "M20.87 7.83 C20.09 8.48 17.65 10.78 15.91 12.0 C14.17 13.22 11.52 14.44 10.43 15.13 "
    "C9.34 15.83 9.56 15.74 9.39 16.17 C9.22 16.61 9.22 17.3 9.39 17.74 C9.56 18.17 10.82 18.61 10.43 18.78 "
    "C10.04 18.95 7.78 19.04 7.04 18.78 C6.3 18.52 6.13 17.7 6.0 17.22 C5.87 16.74 5.83 16.39 6.26 15.91 "
    "C6.69 15.43 7.48 14.87 8.61 14.35 C9.74 13.83 11.78 13.3 13.04 12.78 C14.3 12.26 14.91 12.0 16.17 11.22 "
    "C17.43 10.44 19.83 8.65 20.61 8.09 C21.39 7.52 21.65 7.18 20.87 7.83 Z"
)
# 尾迹圆点(cx, cy, r),沿弧线由大到小
_HLTV_DOTS = (
    (4.67, 17.50, 0.78),
    (4.30, 13.27, 0.66),
    (5.97, 9.27, 0.62),
    (9.00, 6.78, 0.52),
    (12.26, 6.45, 0.46),
    (14.87, 7.04, 0.42),
    (16.57, 8.22, 0.34),
)
# 把描摹坐标(含圆点)整体居中并放大填满图标框
_HLTV_TF = "translate(12 12) scale(1.26) translate(-12.24 -12.48)"


def _help_badge(size: int = 64) -> str:
    """赤陶圆角方块 + 白色 HLTV 流星标识(照官方 logo 描摹的矢量,按卡片配色改色)。"""
    icon = round(size * 0.52)
    dots = "".join(f'<circle cx="{x}" cy="{y}" r="{r}"/>' for x, y, r in _HLTV_DOTS)
    return (
        f'<span style="width:{size}px;height:{size}px;border-radius:{round(size * 0.28)}px;'
        f"background:{ACCENT};display:inline-flex;align-items:center;justify-content:center;"
        f'flex:none;box-shadow:0 5px 14px -5px rgba(196,112,78,0.65);">'
        f'<svg width="{icon}" height="{icon}" viewBox="0 0 24 24" fill="{CARD}">'
        f'<g transform="{_HLTV_TF}"><path d="{_HLTV_SWOOSH}"/>{dots}</g>'
        f"</svg></span>"
    )


def _cmd_pill(cmd: str) -> str:
    return (
        f'<span style="display:inline-block;background:{INNER};border:1px solid {BORDER};'
        f"border-radius:9px;padding:7px 14px;font-size:17px;font-weight:600;color:{ACCENT_D};"
        f'white-space:nowrap;">{_esc(cmd)}</span>'
    )


def _help_tag(text: str) -> str:
    return (
        f'<span style="display:inline-block;margin-left:10px;background:{PANEL};'
        f"border-radius:999px;padding:3px 11px;font-size:12.5px;font-weight:600;"
        f'color:{MUTE};vertical-align:middle;white-space:nowrap;">{_esc(text)}</span>'
    )


def _cmd_row(cmd: str, desc: str, last: bool, tag: str = "") -> str:
    # 命令列宽要容得下最长的 pill(/cs2 订阅 战队 <名字>),否则会盖住右侧描述
    bb = "" if last else f"border-bottom:1px solid {BORDER_R};"
    return (
        f'<div style="display:flex;align-items:center;gap:22px;padding:15px 4px;{bb}">'
        f'<div style="width:228px;flex:none;">{_cmd_pill(cmd)}</div>'
        f'<div style="flex:1;min-width:0;font-size:17px;color:{ROW};line-height:1.4;">'
        f"{_esc(desc)}{_help_tag(tag) if tag else ''}</div></div>"
    )


def _help_section(cn: str, en: str, rows: str) -> str:
    return (
        f'<div style="margin-top:28px;">'
        f'<div style="display:flex;align-items:baseline;gap:10px;padding-bottom:4px;">'
        f'<span style="font-size:18px;font-weight:700;color:{INK};letter-spacing:-0.2px;">{_esc(cn)}</span>'
        f'<span style="font-size:13px;font-weight:500;color:{MUTE};letter-spacing:0.3px;">{_esc(en)}</span></div>'
        f'<div style="border-top:1px solid {LINE};">{rows}</div></div>'
    )


def _callout_row(title: str, text: str) -> str:
    return (
        f'<div style="display:flex;align-items:flex-start;gap:13px;">'
        f'<span style="width:9px;height:9px;border-radius:50%;background:{ACCENT};margin-top:6px;flex:none;"></span>'
        f'<div style="font-size:15.5px;color:{ROW};line-height:1.55;">'
        f'<span style="font-weight:700;color:{ACCENT_D};">{_esc(title)}</span> — {_esc(text)}</div></div>'
    )


def build_help_html(admin: bool, when_text: str) -> str:
    callout = (
        f'<div style="margin-top:24px;background:{ACCENT_BG};border:1px solid rgba(196,112,78,0.22);'
        f'border-radius:16px;padding:16px 20px;display:flex;flex-direction:column;gap:12px;">'
        + _callout_row(
            "自动播报",
            "顶级赛事直播时,每打完一张地图即时推送一张战报卡(大场比分 + 双方十人 Rating)"
            "到订阅群,无需手动查询。",
        )
        + _callout_row(
            "订阅 @ 到人",
            "订阅战队 / 选手后,不限赛事级别,每场比赛的开赛提醒和每张地图赛果"
            "都会在群里 @ 你。",
        )
        + "</div>"
    )

    query_rows = (
        _cmd_row("/cs2 赛事", "未来 3 个月的顶级赛事一览", False)
        + _cmd_row("/cs2 日程", "今日关注赛事的比赛 · 赛果 / 直播 / 待开始;无赛则看下个比赛日", False)
        + _cmd_row("/cs2 赛程", "正在进行 / 即将开赛赛事的完整对阵 · 小组赛 / 淘汰赛,可指定其一", True)
    )
    sub_rows = (
        _cmd_row("/cs2 订阅", "把本群加入自动战报推送", False, tag="群管理员")
        + _cmd_row("/cs2 订阅 战队 <名字>", "订阅某战队,开赛和赛果都会在群里 @ 你", False)
        + _cmd_row("/cs2 订阅 选手 <名字>", "订阅某选手(s1mple / m0nesy 这类写法都认)", False)
        + _cmd_row("/cs2 我的订阅", "查看你在本群订阅的战队 / 选手", False)
        + _cmd_row("/cs2 退订 …", "退订本群,或退订某战队 / 选手", True)
    )
    sections = _help_section("查询", "Query", query_rows) + _help_section(
        "订阅", "Subscribe", sub_rows
    )
    if admin:
        admin_rows = (
            _cmd_row("/cs2 状态", "运行状态、白名单与订阅群信息", False, tag="仅调试群")
            + _cmd_row("/cs2 测试", "立即渲染一张战报卡,可带比赛 ID / URL", False, tag="仅调试群")
            + _cmd_row("/cs2 重试投递", "重新激活全部或指定比赛的死信", False, tag="仅调试群")
            + _cmd_row("/cs2 刷新名录", "强制刷新战队 / 选手名录(抓一次世界排行榜)", False, tag="仅调试群")
            + _cmd_row("/cs2 刷新VRS", "强制刷新 Valve 世界排名总榜(平时每天自动抓一次)", True, tag="仅调试群")
        )
        sections += _help_section("管理 · 调试", "Admin", admin_rows)

    body = f"""
  <div style="display:flex;align-items:center;gap:18px;">
   {_help_badge(64)}
   <div style="flex:1;min-width:0;display:flex;flex-direction:column;gap:4px;">
    <div style="font-size:30px;font-weight:700;color:{INK};letter-spacing:-0.4px;line-height:1.1;">CS2 战报机器人</div>
    <div style="font-size:17px;color:{SUB};font-weight:500;">HLTV 顶级赛事 · 逐图战报播报</div>
   </div>
   <div style="font-size:14px;color:{MUTE};font-weight:500;white-space:nowrap;padding-top:4px;">前缀 <span style="color:{ACCENT};font-weight:700;">/cs2</span></div>
  </div>
  {callout}
{sections}
  <div style="display:flex;align-items:center;justify-content:space-between;margin-top:32px;
       padding-top:20px;border-top:1px solid {BORDER};">
   <div style="font-size:14px;color:{FAINT};">命令 /cs2 或 cs2 均可触发 · {_esc(when_text)}</div>
   <div style="display:flex;align-items:center;gap:18px;font-size:14px;color:{MUTE};">
    <div>开源 · <span style="color:{ACCENT};font-weight:600;">github.com/canxiaocai/cs2-event-bot</span></div>
    <div>数据 · <span style="color:{ACCENT};font-weight:600;">hltv.org</span></div>
   </div>
  </div>"""
    return _shell(body, "44px 48px 38px")


async def render_help_card(admin: bool, when_text: str) -> bytes:
    return await _render_png(build_help_html(admin, when_text))
