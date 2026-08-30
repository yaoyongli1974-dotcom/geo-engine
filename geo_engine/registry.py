"""组件注册中心 —— 系统的扩展点。

所有可插拔组件（Reader / Enhancer / Formatter / Publisher / Probe）都通过
装饰器注册进来，业务线配置里只用写字符串 type，即可零改动接入新实现。

用法::

    from geo_engine.registry import REGISTRY

    @REGISTRY.publisher("feishu")
    class FeishuPublisher(BasePublisher):
        ...

    cls = REGISTRY.get("publisher", "feishu")
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Type

# 支持的组件类别
CATEGORIES = ("reader", "chunker", "extractor", "enhancer", "formatter", "publisher", "probe")


class Registry:
    """极简组件注册表。"""

    def __init__(self) -> None:
        self._store: Dict[str, Dict[str, Type[Any]]] = {c: {} for c in CATEGORIES}

    # ---- 装饰器 API ----
    def register(self, category: str, name: str) -> Callable[[Type[Any]], Type[Any]]:
        if category not in self._store:
            # 允许第三方扩展自定义类别
            self._store[category] = {}

        def _wrap(cls: Type[Any]) -> Type[Any]:
            if name in self._store[category]:
                raise ValueError(f"组件重复注册: {category}/{name}")
            self._store[category][name] = cls
            cls.component_type = name      # type: ignore[attr-defined]
            cls.component_category = category  # type: ignore[attr-defined]
            return cls

        return _wrap

    def reader(self, name: str):        return self.register("reader", name)
    def chunker(self, name: str):       return self.register("chunker", name)
    def extractor(self, name: str):     return self.register("extractor", name)
    def enhancer(self, name: str):      return self.register("enhancer", name)
    def formatter(self, name: str):     return self.register("formatter", name)
    def publisher(self, name: str):     return self.register("publisher", name)
    def probe(self, name: str):         return self.register("probe", name)

    # ---- 查询 API ----
    def get(self, category: str, name: str) -> Type[Any]:
        try:
            return self._store[category][name]
        except KeyError:
            available = ", ".join(sorted(self._store.get(category, {}))) or "无"
            raise KeyError(f"未注册的{category}组件: '{name}'；已注册: {available}") from None

    def has(self, category: str, name: str) -> bool:
        return name in self._store.get(category, {})

    def list(self, category: str) -> List[str]:
        return sorted(self._store.get(category, {}))

    def all(self) -> Dict[str, List[str]]:
        return {c: self.list(c) for c in sorted(self._store)}


REGISTRY = Registry()
