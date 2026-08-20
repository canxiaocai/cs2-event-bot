"""Major 冠军名录(静态数据,半年才动一次,不联网查)。

用途:战报卡/开赛卡在**拿过 Major 冠军**的战队名与选手昵称后面标星(几冠几颗星)。

口径(用户确认):
- 算**全部 25 届** Major(CS:GO 2013 起 + CS2),ESL One Rio 2020 因疫情取消不计。
- 战队按**俱乐部**算:Outsiders 就是禁赛期改名的 Virtus.pro,两冠都记在 VP 名下。
  只按夺冠时的队名归属,不做血缘继承(如 SK/Luminosity 的巴西阵后来去了 MIBR,
  MIBR 不因此得星)。
- 选手按**昵称**算,大小写无关的**精确**匹配(不做 names.py 的 leet 还原——
  那是给用户输入用的模糊匹配,这里若把 b1t 归一成 bit 反而可能误标别人)。

**新增一届 Major 时:只需在 MAJORS 末尾追加一条**,战队若是新队再去 _TEAM_ALIASES
补一行别名(键为 HLTV 上显示的队名)。其余全部自动。
"""

from __future__ import annotations

from typing import NamedTuple


class Major(NamedTuple):
    year: int
    name: str  # 赛事名(仅供人看/排查)
    team: str  # 夺冠俱乐部(规范名,见 _TEAM_ALIASES 的键)
    players: tuple[str, ...]  # 夺冠首发五人昵称(教练不计)


# 按时间顺序;末尾追加即可。
MAJORS: tuple[Major, ...] = (
    Major(2013, "DreamHack Winter 2013", "Fnatic",
          ("JW", "flusha", "pronax", "schneider", "Devilwalk")),
    Major(2014, "EMS One Katowice 2014", "Virtus.pro",
          ("NEO", "TaZ", "pashaBiceps", "Snax", "byali")),
    Major(2014, "ESL One Cologne 2014", "Ninjas in Pyjamas",
          ("f0rest", "GeT_RiGhT", "Xizt", "friberg", "Fifflaren")),
    Major(2014, "DreamHack Winter 2014", "LDLC",
          ("shox", "SmithZz", "Happy", "NBK-", "kioShiMa")),
    Major(2015, "ESL One Katowice 2015", "Fnatic",
          ("olofmeister", "flusha", "JW", "KRIMZ", "pronax")),
    Major(2015, "ESL One Cologne 2015", "Fnatic",
          ("olofmeister", "flusha", "JW", "KRIMZ", "pronax")),
    Major(2015, "DreamHack Cluj-Napoca 2015", "EnVyUs",
          ("kennyS", "apEX", "NBK-", "Happy", "kioShiMa")),
    Major(2016, "MLG Columbus 2016", "Luminosity",
          ("FalleN", "coldzera", "fer", "TACO", "fnx")),
    Major(2016, "ESL One Cologne 2016", "SK Gaming",
          ("FalleN", "coldzera", "fer", "TACO", "fnx")),
    Major(2017, "ELEAGUE Major Atlanta 2017", "Astralis",
          ("device", "dupreeh", "Xyp9x", "gla1ve", "Kjaerbye")),
    Major(2017, "PGL Major Kraków 2017", "Gambit",
          ("AdreN", "Dosia", "HObbit", "mou", "Zeus")),
    Major(2018, "ELEAGUE Major Boston 2018", "Cloud9",
          ("Stewie2K", "tarik", "autimatic", "RUSH", "Skadoodle")),
    Major(2018, "FACEIT Major London 2018", "Astralis",
          ("device", "dupreeh", "Xyp9x", "gla1ve", "Magisk")),
    Major(2019, "IEM Katowice Major 2019", "Astralis",
          ("device", "dupreeh", "Xyp9x", "gla1ve", "Magisk")),
    Major(2019, "StarLadder Berlin Major 2019", "Astralis",
          ("device", "dupreeh", "Xyp9x", "gla1ve", "Magisk")),
    Major(2021, "PGL Major Stockholm 2021", "Natus Vincere",
          ("s1mple", "electronic", "b1t", "Perfecto", "Boombl4")),
    Major(2022, "PGL Major Antwerp 2022", "FaZe",
          ("karrigan", "rain", "Twistzz", "ropz", "broky")),
    Major(2022, "IEM Rio Major 2022", "Virtus.pro",  # 当时名为 Outsiders
          ("Jame", "FL1T", "n0rb3r7", "fame", "Qikert")),
    Major(2023, "BLAST.tv Paris Major 2023", "Vitality",
          ("ZywOo", "apEX", "Magisk", "dupreeh", "Spinx")),
    Major(2024, "PGL Major Copenhagen 2024", "Natus Vincere",
          ("Aleksib", "iM", "b1t", "jL", "w0nderful")),
    Major(2024, "Perfect World Shanghai Major 2024", "Spirit",
          ("donk", "sh1ro", "chopper", "magixx", "zont1x")),
    Major(2025, "BLAST.tv Austin Major 2025", "Vitality",
          ("ZywOo", "apEX", "flameZ", "mezii", "ropz")),
    Major(2025, "StarLadder Budapest Major 2025", "Vitality",
          ("ZywOo", "apEX", "flameZ", "mezii", "ropz")),
    Major(2026, "IEM Cologne Major 2026", "Falcons",
          ("NiKo", "m0NESY", "TeSeS", "kyousuke", "karrigan")),
)

# 规范队名 → HLTV 上可能显示的写法(全部小写比较)。HLTV 现用名放第一个。
_TEAM_ALIASES: dict[str, tuple[str, ...]] = {
    "Fnatic": ("fnatic",),
    "Virtus.pro": ("virtus.pro", "virtus pro", "outsiders"),
    "Ninjas in Pyjamas": ("nip", "ninjas in pyjamas"),
    "LDLC": ("ldlc", "team ldlc", "ldlc.com", "team ldlc.com"),
    "EnVyUs": ("envyus", "team envyus", "nv"),
    "Luminosity": ("luminosity", "luminosity gaming"),
    "SK Gaming": ("sk gaming", "sk"),
    "Astralis": ("astralis",),
    "Gambit": ("gambit", "gambit esports", "gambit gaming"),
    "Cloud9": ("cloud9", "c9"),
    "Natus Vincere": ("natus vincere", "navi", "na'vi"),
    "FaZe": ("faze", "faze clan"),
    "Vitality": ("vitality", "team vitality"),
    "Spirit": ("spirit", "team spirit"),
    "Falcons": ("falcons", "team falcons"),
}


def _build() -> tuple[dict[str, int], dict[str, int]]:
    teams: dict[str, int] = {}
    players: dict[str, int] = {}
    for mj in MAJORS:
        for alias in _TEAM_ALIASES.get(mj.team, (mj.team.lower(),)):
            teams[alias] = teams.get(alias, 0) + 1
        for nick in mj.players:
            k = nick.casefold()
            players[k] = players.get(k, 0) + 1
    return teams, players


_TEAM_TITLES, _PLAYER_TITLES = _build()

# 昵称写法变体 → 名录里的昵称(HLTV 改过写法/带后缀的少数几个)。
_NICK_ALIASES = {
    "dev1ce": "device",
    "nbk": "NBK-",
    "get_right": "GeT_RiGhT",
    "getright": "GeT_RiGhT",
    "olofm": "olofmeister",
}


def team_titles(name: str) -> int:
    """该战队(按俱乐部)拿过几次 Major 冠军;没拿过返回 0。"""
    return _TEAM_TITLES.get((name or "").strip().casefold(), 0)


def player_titles(nick: str) -> int:
    """该选手拿过几次 Major 冠军;没拿过返回 0。"""
    k = (nick or "").strip().casefold()
    k = _NICK_ALIASES.get(k, k).casefold()
    return _PLAYER_TITLES.get(k, 0)
