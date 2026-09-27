from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Stream:
    """單一播放地址"""
    name: str
    url: str
    tvg_id: str = ""
    tvg_name: str = ""
    logo: str = ""
    group: str = ""
    source: str = ""
    headers: dict = field(default_factory=dict)
    country_hint: str = ""     # 來源推測/指定的國家

    # ---- 匹配結果 ----
    key: str = ""              # 正規化後的比對鍵
    display: str = ""          # 輸出用顯示名稱
    alias_hit: bool = False    # 是否命中別名表
    channel_id: str = ""       # iptv-org 頻道 ID
    country: str = ""
    categories: list = field(default_factory=list)
    closed: bool = False
    nsfw: bool = False

    # ---- 效驗 / 測速結果 ----
    ok: Optional[bool] = None
    latency: Optional[float] = None   # 秒
    speed: Optional[float] = None     # KB/s
    width: int = 0
    height: int = 0
    error: str = ""

    @property
    def group_key(self) -> str:
        return self.channel_id or f"key:{self.key}"


@dataclass
class Channel:
    """聚合後的頻道(可含多條線路)"""
    gid: str
    display: str
    key: str
    channel_id: str = ""
    logo: str = ""
    country: str = ""
    categories: list = field(default_factory=list)
    group: str = ""
    tvg_id: str = ""
    streams: list = field(default_factory=list)
