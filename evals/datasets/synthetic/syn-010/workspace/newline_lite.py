"""把任意换行风格归一成 LF。

日志来自 Windows（CRLF）、老 Mac（CR）与 Unix（LF）三路采集，
入库前必须统一成 LF，否则按行切分会出"幽灵空行"。
"""

from __future__ import annotations


def normalize(text: str) -> str:
    return text.replace("\r", "\n")
