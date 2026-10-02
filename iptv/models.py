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
    logos: list = field(default_factory=list)  # 台標候選(依優先順序),由 logos.py 擇優後寫入 logo
    db_logo: str = ""                           # 頻道資料庫給這個頻道的台標
    country: str = ""
    categories: list = field(default_factory=list)
    group: str = ""
    tvg_id: str = ""
    streams: list = field(default_factory=list)
    alias_hit: bool = False  # 顯示名稱是否命中人工別名表(已經是人工判斷過的固定名稱,稽核不需要再建議改名)
