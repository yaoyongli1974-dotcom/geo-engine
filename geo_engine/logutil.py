"""轻量日志封装（标准库 logging，中文友好输出）。"""

from __future__ import annotations

import logging
import sys
from typing import Optional

_CONFIGURED = False


def get_logger(name: str = "geo", level: Optional[str] = None):
    global _CONFIGURED
    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%H:%M:%S",
        ))
        root = logging.getLogger("geo")
        root.addHandler(handler)
        root.setLevel(getattr(logging, (level or "INFO").upper(), logging.INFO))
        root.propagate = False
        _CONFIGURED = True
    lg = logging.getLogger(f"geo.{name}")
    if level:
        logging.getLogger("geo").setLevel(getattr(logging, level.upper(), logging.INFO))
    return lg
