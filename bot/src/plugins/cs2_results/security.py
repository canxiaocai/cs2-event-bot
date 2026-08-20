"""输入边界校验。

所有会进入 Playwright 的用户输入必须先在这里收敛成受信 URL；抓取器本身还会做
第二层 host/redirect 校验，形成纵深防御。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from .hltv import BASE

_MATCH_PATH = re.compile(r"^/matches/(?P<id>[0-9]+)(?:/[^/?#]*)?/?$", re.ASCII)


def hltv_match_url(value: str) -> str | None:
    """把比赛 ID 或 HLTV 比赛 URL 规范化；其他输入一律拒绝。"""
    raw = (value or "").strip()
    if not raw.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        return None
    if raw and all("0" <= char <= "9" for char in raw):
        return f"{BASE}/matches/{raw}/x"

    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname != "www.hltv.org":
        return None
    if parsed.username or parsed.password or port not in (None, 443):
        return None
    if not _MATCH_PATH.fullmatch(parsed.path):
        return None
    return urlunsplit(("https", "www.hltv.org", parsed.path, "", ""))
