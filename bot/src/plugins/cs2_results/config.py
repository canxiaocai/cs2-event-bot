"""cs2_results 插件配置。

所有项都可在 bot/.env 里用大写同名键覆盖,例如:
    CS2_SUBSCRIBED_GROUPS=[123456789]
    CS2_LIVE_POLL_INTERVAL=2
"""

from pydantic import BaseModel, Field, field_validator, model_validator


class Config(BaseModel):
    # 预置推送目标群(会和运行时 /cs2 订阅 的群合并)
    cs2_subscribed_groups: list[int] = Field(default_factory=list)

    # 调试群:只有这些群(或超管私聊)才能用管理/调试命令(/cs2 状态、/cs2 测试)、
    # 看到「管理·调试」版帮助卡。普通群完全看不到这些功能,也不会被泄露订阅信息。
    cs2_debug_groups: list[int] = Field(default_factory=list)

    # —— 轮询频率 ——
    cs2_live_poll_interval: int = Field(default=2, ge=1, le=1440)
    cs2_matches_scan_interval: int = Field(default=3, ge=1, le=1440)
    cs2_max_followed: int = Field(default=10, ge=1, le=100)  # 超过此并发直播数时发容量告警
    cs2_featured_refresh_hour: int = Field(default=4, ge=0, le=23)
    cs2_featured_sticky_days: int = Field(default=3, ge=0)
    # 赛事「正在进行」判定:除 HLTV #FEATURED 外,距首个比赛日 ≤ 此天数的即将开赛赛事
    # 也算正在进行(/cs2 赛程 便能在开赛前几天就查到,如开赛前 3 天的 BLAST Bounty)。
    cs2_ongoing_lead_days: int = Field(default=3, ge=0, le=30)
    # 与 cs2_cache_event_page_ttl(默认 300s)对齐:过期即刷,避免 warm 常命中 max_age 空转
    cs2_event_warm_interval: int = Field(default=5, ge=1, le=1440)
    cs2_event_warm_cap: int = Field(default=2, ge=1, le=100)

    # —— /cs2 日程 的「比赛日」切分 ——
    # 一个比赛日按**时间空档聚类**得出,而不是日历日:欧洲赛事常 18:00 开打、跨午夜到
    # 次日 01:30,按日历日切会把同一晚的末场甩到"明天"的卡上;凌晨查更糟(刚过 0 点,
    # "今天"既丢了正在打的那半场,又把十几小时后的下一场算进来)。相邻两场开赛间隔超过
    # gap 就切一刀(实测:日内场间隔 2.5h、日间空档 15.5–17.5h,6h 阈值余量很大);
    # max_span 是兜底,防 ESL 那种多线并行赛事把整周连成一段。
    cs2_match_day_gap_hours: float = Field(default=6.0, ge=1, le=24)
    cs2_match_day_max_span_hours: float = Field(default=20.0, ge=6, le=48)
    # 末场开赛后再算 tail 小时仍属"进行中"(BO3 打完约需这么久),避免末场刚开就跳到下一天。
    # 只对**还有未完场次/直播**的比赛日生效(见 _handle_schedule):整段打完就直接翻页,
    # 战报走 recap 折叠,所以 tail 放宽只是给长盘 BO3/BO5 更多余量,不会压住下一天的排期。
    cs2_match_day_tail_hours: float = Field(default=6.0, ge=0, le=12)
    # 空档期在下个比赛日上方附带的"上一比赛日战报";超过此时长的旧战报不再附带
    cs2_recap_max_age_hours: float = Field(default=24.0, ge=0, le=96)

    # —— /cs2 赛事 的赛事 logo ——
    cs2_events_logo_fetch_cap: int = Field(default=8, ge=1, le=100)
    cs2_events_logo_prewarm_cap: int = Field(default=30, ge=1, le=500)
    # logo 抓取失败(多为图床 Cloudflare 403)后,此冷却秒数内后台不再重试该 logo——
    # 避免每次命令/预热都重撞同一批 403,反而把本机 IP 在图床上的信誉压得更久。0=不冷却。
    cs2_logo_fail_cooldown: int = Field(default=1800, ge=0)

    # —— 缓存(页面 HTML + logo 字节;直播轮询始终拉新,但会回写页面缓存)——
    cs2_cache_matches_ttl: int = Field(default=180, ge=0)
    cs2_cache_events_ttl: int = Field(default=21600, ge=0)
    cs2_cache_results_ttl: int = Field(default=600, ge=0)
    cs2_cache_event_page_ttl: int = Field(default=300, ge=0)
    cs2_logo_keep_days: int = Field(default=45, ge=1)
    cs2_cache_cleanup_hour: int = Field(default=5, ge=0, le=23)

    # —— 陈旧缓存兜底(stale-while-revalidate):命令场景缓存过期但在窗口内 → 先秒回旧副本,
    # 后台异步刷新,下次即新。请求总量不变,只是把"用户等"换成"后台做"。 ——
    cs2_stale_matches: int = Field(default=1800, ge=0)
    cs2_stale_results: int = Field(default=3600, ge=0)
    cs2_stale_events: int = Field(default=172800, ge=0)
    cs2_stale_event_page: int = Field(default=3600, ge=0)

    # —— 顶级赛事白名单手动覆盖(HLTV 事件 id,字符串)——
    cs2_force_include_events: list[str] = Field(default_factory=list)
    cs2_force_exclude_events: list[str] = Field(default_factory=list)

    # —— 抓取(反爬)——
    cs2_headful: bool = False  # 调试时可设 True 看浏览器窗口
    # 两次**抓取**之间的最小间隔。注意语义:一次抓取 = 一次 goto + (几乎必然的) 一次
    # 挑战 reload,这两下共用一个档位(见 fetcher._navigate_html),所以实际请求速率约为
    # 每 min_gap 两个请求。
    cs2_request_min_gap: float = Field(default=2.5, ge=0, le=3600)
    cs2_nav_timeout: int = Field(default=45000, ge=1000, le=120000)
    cs2_challenge_retries: int = Field(default=3, ge=1, le=10)
    # 认出 Cloudflare 挑战页后,给它多久「自行放行」的宽限;超时就立刻 reload。
    # 实测挑战页干等 40s 也不放行,而 reload 1.5~1.7s 必过,所以这个窗口只是兜底,
    # 别调大——每次抓页都会付这份钱。
    cs2_challenge_grace_ms: int = Field(default=1500, ge=0, le=30000)
    # 单次抓取(含挑战重试)占用导航档位的时间预算(秒)。超预算就不再重试,免得
    # HLTV 超时时连续几个 45s 导航把闸门长占,堵住用户命令和直播轮询。
    cs2_fetch_budget_seconds: float = Field(default=60.0, ge=5, le=600)

    # —— 兜底 & 告警 ——
    cs2_results_backstop: bool = True  # 补报:重启/离线/漏扫期间结束的比赛,发现后补推
    cs2_backstop_window_min: int = Field(default=120, ge=1)
    # 进程启动后第一次 /results 补报用更宽窗口,兜住关机过夜等长离线
    cs2_startup_backstop_window_min: int = Field(default=720, ge=1)
    # /matches 扫描可复用最近命令/SWR 刚拉过的页面缓存(秒);0=始终真抓
    cs2_scan_cache_max_age: int = Field(default=45, ge=0, le=600)
    # 追踪中的比赛超过此时长且已离开 /matches 仍无完赛/无 pending 评分 → 放弃(小时)
    cs2_stuck_follow_hours: float = Field(default=4.0, ge=1.0, le=48.0)
    cs2_alert_after_failures: int = Field(default=5, ge=1)

    # —— 战队/选手订阅(个人级,@ 到人)——
    # 只有已开启群级 /cs2 订阅 的群,成员才能再订阅具体战队/选手。
    cs2_sub_any_tier: bool = True  # True=订阅对象的任何赛事都追;False=仅顶级白名单赛事
    cs2_sub_max_targets_per_user: int = Field(default=20, ge=1, le=200)  # 每人每群订阅上限
    # 开赛卡:比赛距开赛≤此分钟数时预解析阵容(拿首发,供选手命中确认);0=只在真开赛后解析
    cs2_sub_start_window_min: int = Field(default=30, ge=0, le=720)
    # 每个扫描周期为「选手命中确认/阵容预解析」最多真抓多少场比赛页(封顶 ban 风险)
    cs2_sub_lineup_resolve_cap: int = Field(default=2, ge=0, le=20)
    # 选手当前所属队(team_hint)的复查间隔;命中主要靠 team_hint 走 /matches 零成本
    cs2_sub_player_team_refresh_hours: float = Field(default=24.0, ge=1.0, le=720.0)
    # 首次看到订阅比赛已在直播、但已打完的图数超过此值 → 视为错过开赛,不再补发「开赛」卡
    cs2_sub_start_skip_if_maps_done: int = Field(default=1, ge=1, le=5)
    # 本地名录(订阅命令查它,不实时打 HLTV):/ranking/teams 全量刷新间隔(小时)。
    # 每次刷新仅 1 个请求(~226 队 + 全部现役阵容);追踪比赛还会顺路收集保持新鲜。
    cs2_roster_refresh_hours: float = Field(default=72.0, ge=1.0, le=24 * 30)

    # —— Valve 世界排名(VRS):卡片上队名旁的 #N ——
    # HLTV 的 /valve-ranking/teams 是**每天一版**的快照(URL 会重定向到当日日期),一次请求
    # 就拿到全榜 ~389 队,所以正确的节奏是「每天真抓一次」,绝不能跟着比赛结果去轮询。
    # 三道闸门共同保证这一点(见 __init__.refresh_vrs_ranking):
    #   1) 命令路径永不现抓——只读本地表,没有就不显示名次,决不让用户等;
    #   2) max_age 未到直接 no-op;min_gap 是硬闸,无论谁触发都不会比它更密;
    #   3) 抓失败进冷却,避免撞上 Cloudflare 后反复重试把本机 IP 的信誉压得更久。
    # 比赛结果带来的名次变化不靠刷这张榜:追踪比赛时会从比赛页 VRS 面板顺路收下实时名次
    # (零额外请求),赛果一出就更新——那才是「随每场比赛更新」的正解。
    cs2_vrs_enabled: bool = True
    cs2_vrs_refresh_hour: int = Field(default=6, ge=0, le=23)  # 每日全量刷新时刻(北京时间)
    cs2_vrs_max_age_hours: float = Field(default=20.0, ge=1, le=24 * 30)  # 超龄才允许后台补刷
    cs2_vrs_min_gap_hours: float = Field(default=6.0, ge=0.5, le=24 * 7)  # 两次真抓的最小间隔
    cs2_vrs_fail_cooldown_min: int = Field(default=60, ge=1)  # 抓失败后的冷却分钟数

    # —— 命令保护 / 投递重试 ——
    cs2_command_cooldown: float = Field(default=5.0, ge=0, le=300)
    cs2_delivery_max_attempts: int = Field(default=5, ge=1, le=20)
    cs2_delivery_retry_base_seconds: int = Field(default=30, ge=1, le=86400)
    cs2_delivery_poll_seconds: float = Field(default=2.0, ge=0.5, le=60)
    cs2_delivery_keep_days: int = Field(default=30, ge=1, le=3650)

    # —— 群禁言(全员禁言 / 机器人被禁言)——
    # 撞上禁言后该群进入静默期:期间的战报只**顺延**、不消耗重试次数,免得每张卡都空跑
    # 5 次指数退避再进死信(实测某群开了全员禁言,每场比赛的每张图都要白跑一轮)。
    # 收到解禁通知会立刻清掉静默期,不必等它自然到期。
    cs2_mute_backoff_minutes: int = Field(default=15, ge=1, le=1440)
    # 禁言期间战报最多顺延多久,超时直接丢弃(不告警)。隔夜的战报补发出来没意义,
    # 而且会在解禁那一刻一次性刷屏。
    cs2_mute_defer_max_hours: float = Field(default=6.0, ge=0.5, le=72)

    @field_validator("cs2_subscribed_groups", "cs2_debug_groups")
    @classmethod
    def validate_group_ids(cls, values: list[int]) -> list[int]:
        if any(value <= 0 for value in values):
            raise ValueError("QQ群号必须是正整数")
        return list(dict.fromkeys(values))

    @field_validator("cs2_force_include_events", "cs2_force_exclude_events")
    @classmethod
    def validate_event_ids(cls, values: list[str]) -> list[str]:
        if any(not value or not value.isascii() or not value.isdecimal() for value in values):
            raise ValueError("HLTV event id 必须是 ASCII 数字")
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def validate_cache_windows(self) -> "Config":
        pairs = (
            ("cs2_stale_matches", self.cs2_stale_matches, self.cs2_cache_matches_ttl),
            ("cs2_stale_results", self.cs2_stale_results, self.cs2_cache_results_ttl),
            ("cs2_stale_events", self.cs2_stale_events, self.cs2_cache_events_ttl),
            ("cs2_stale_event_page", self.cs2_stale_event_page, self.cs2_cache_event_page_ttl),
        )
        for name, stale, fresh in pairs:
            if stale and stale < fresh:
                raise ValueError(f"{name} 必须为 0 或不小于对应 fresh TTL")
        if self.cs2_startup_backstop_window_min < self.cs2_backstop_window_min:
            raise ValueError("cs2_startup_backstop_window_min 不能小于 cs2_backstop_window_min")
        if self.cs2_vrs_min_gap_hours > self.cs2_vrs_max_age_hours:
            raise ValueError("cs2_vrs_min_gap_hours 不能大于 cs2_vrs_max_age_hours（否则永不刷新）")
        return self
