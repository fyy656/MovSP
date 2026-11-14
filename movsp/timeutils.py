from __future__ import annotations

import os
from datetime import datetime

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python 3.8 packaged builds
    from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_TIMEZONE = "Asia/Shanghai"


def env_compat(name: str, default: str = "") -> str:
    """读环境变量，兼容改名前的 JAVSP_WEB_*（详见 storage.env_compat）。"""
    value = os.environ.get(name)
    if value:
        return value
    legacy = "JAVSP_WEB_" + name[len("MOVSP_"):] if name.startswith("MOVSP_") else ""
    if legacy:
        value = os.environ.get(legacy)
        if value:
            return value
    return default


def timezone_name() -> str:
    return env_compat("MOVSP_TIMEZONE", DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE


def local_now() -> datetime:
    try:
        zone = ZoneInfo(timezone_name())
    except ZoneInfoNotFoundError:
        zone = ZoneInfo(DEFAULT_TIMEZONE)
    return datetime.now(zone)


def now_iso() -> str:
    return local_now().isoformat()
