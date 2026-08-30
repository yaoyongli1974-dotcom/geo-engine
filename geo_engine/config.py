"""配置系统 —— 全局配置 + 多业务线配置。

零依赖设计：配置默认使用 JSON；若环境装有 PyYAML，则自动支持 .yaml/.yml。
目录约定::

    <root>/
      config.json              全局配置（可选）
      business_lines/*.json    每个文件一条业务线
      content/<bl_id>/         该业务线的原始内容
      dist/<bl_id>/            该业务线的发布产物
      data/geo.db              SQLite 存储
"""

from __future__ import annotations

import json
import os
from dataclasses import fields, is_dataclass
from typing import Any, Dict, List, Optional

from .models import (
    AuthorityConfig,
    BusinessLine,
    LLMConfig,
    MonitorConfig,
    SourceConfig,
    TargetConfig,
)

DEFAULT_LAYOUT = {
    "business_lines": "business_lines",
    "content": "content",
    "dist": "dist",
    "data": "data",
    "reports": "reports",
}


def load_file(path: str) -> Dict[str, Any]:
    """加载 JSON / YAML 配置文件。"""
    ext = os.path.splitext(path)[1].lower()
    with open(path, "r", encoding="utf-8") as f:
        if ext in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    f"读取 {path} 需要 PyYAML，请改用 JSON 配置或执行 pip install pyyaml"
                ) from exc
            return yaml.safe_load(f) or {}
        return json.load(f) or {}


def dump_config(obj: Any, path: str) -> None:
    """写出配置（JSON，UTF-8，缩进 2）。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    data = obj.to_dict() if hasattr(obj, "to_dict") else obj
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _build_dataclass(cls, data: Any):
    """递归地把 dict 转成 dataclass，忽略未知字段。"""
    if data is None:
        return cls()
    if is_dataclass(data):
        return data
    if not isinstance(data, dict):
        return data
    kwargs: Dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        val = data[f.name]
        if is_dataclass(f.type):
            kwargs[f.name] = _build_dataclass(f.type, val)
        else:
            kwargs[f.name] = val
    return cls(**kwargs)


class Settings:
    """全局设置（含路径布局）。"""

    def __init__(self, root: str, data: Optional[Dict[str, Any]] = None) -> None:
        self.root = os.path.abspath(root)
        data = data or {}
        layout = dict(DEFAULT_LAYOUT)
        layout.update(data.get("layout") or {})
        self.layout = layout
        self.llm = _build_dataclass(LLMConfig, data.get("llm") or {})
        self.monitor = _build_dataclass(MonitorConfig, data.get("monitor") or {})
        self.log_level: str = data.get("log_level", "INFO")
        self.raw: Dict[str, Any] = data

    # ---- 路径助手 ----
    def path(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)

    def bl_dir(self) -> str:
        return self.path(self.layout["business_lines"])

    def content_dir(self, bl_id: str) -> str:
        return self.path(self.layout["content"], bl_id)

    def dist_dir(self, bl_id: str) -> str:
        return self.path(self.layout["dist"], bl_id)

    def report_dir(self, bl_id: str) -> str:
        return self.path(self.layout["reports"], bl_id)

    @property
    def db_path(self) -> str:
        return self.path(self.layout["data"], "geo.db")

    def ensure_dirs(self, bl_id: str = "") -> None:
        for p in (
            self.bl_dir(),
            self.path(self.layout["data"]),
            self.path(self.layout["reports"]),
        ):
            os.makedirs(p, exist_ok=True)
        if bl_id:
            for p in (self.content_dir(bl_id), self.dist_dir(bl_id), self.report_dir(bl_id)):
                os.makedirs(p, exist_ok=True)


def load_business_line(path: str) -> BusinessLine:
    """从文件加载单条业务线配置。"""
    raw = load_file(path)
    bl = _build_dataclass(BusinessLine, raw)
    # 子对象列表需要显式转换
    bl.sources = [_build_dataclass(SourceConfig, s) for s in (raw.get("sources") or [])]
    bl.targets = [_build_dataclass(TargetConfig, t) for t in (raw.get("targets") or [])]
    bl.authority = _build_dataclass(AuthorityConfig, raw.get("authority") or {})
    bl.llm = _build_dataclass(LLMConfig, raw.get("llm") or {})
    bl.monitor = _build_dataclass(MonitorConfig, raw.get("monitor") or {})
    if not bl.id:
        bl.id = os.path.splitext(os.path.basename(path))[0]
    return bl


class ConfigRepository:
    """配置仓储：负责加载/列出/保存所有业务线。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def list_ids(self) -> List[str]:
        d = self.settings.bl_dir()
        if not os.path.isdir(d):
            return []
        out = []
        for name in sorted(os.listdir(d)):
            if os.path.splitext(name)[1].lower() in (".json", ".yaml", ".yml"):
                out.append(os.path.splitext(name)[0])
        return out

    def load_all(self) -> List[BusinessLine]:
        d = self.settings.bl_dir()
        items: List[BusinessLine] = []
        for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            ext = os.path.splitext(name)[1].lower()
            if ext in (".json", ".yaml", ".yml"):
                items.append(load_business_line(os.path.join(d, name)))
        return items

    def load(self, bl_id: str) -> BusinessLine:
        d = self.settings.bl_dir()
        for ext in (".json", ".yaml", ".yml"):
            p = os.path.join(d, bl_id + ext)
            if os.path.isfile(p):
                return load_business_line(p)
        raise FileNotFoundError(f"找不到业务线配置: {bl_id}（查找目录 {d}）")

    def save(self, bl: BusinessLine) -> str:
        os.makedirs(self.settings.bl_dir(), exist_ok=True)
        path = os.path.join(self.settings.bl_dir(), f"{bl.id}.json")
        dump_config(bl, path)
        return path


def load_settings(root: str) -> Settings:
    for name in ("config.json", "config.yaml", "config.yml"):
        p = os.path.join(root, name)
        if os.path.isfile(p):
            return Settings(root, load_file(p))
    return Settings(root, {})
