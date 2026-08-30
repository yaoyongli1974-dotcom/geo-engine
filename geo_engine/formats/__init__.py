"""标准化输出包 —— 把结构化资产渲染成生成式引擎易抓取、易引用的格式。"""

from .jsonld import JSONLDBuilder          # noqa: F401
from .llms_txt import LlmsTxtBuilder       # noqa: F401
from .cards import CardBuilder             # noqa: F401
from .site import SiteBuilder, build_site  # noqa: F401

__all__ = ["JSONLDBuilder", "LlmsTxtBuilder", "CardBuilder", "SiteBuilder", "build_site"]
