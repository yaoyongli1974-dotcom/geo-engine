"""内容接入层 —— 把分散的企业资料统一读成 SourceDoc。

内置 Reader：markdown_dir / file / text / csv / jsonl / url / api
扩展方式::

    @REGISTRY.reader("feishu")
    class FeishuReader(BaseReader):
        def read(self) -> List[SourceDoc]: ...
"""

from __future__ import annotations

import csv
import json
import os
import re
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, List, Optional

from .logutil import get_logger
from .models import BusinessLine, SourceConfig, SourceDoc, utcnow
from .registry import REGISTRY

log = get_logger("ingest")

TEXT_EXT = {".md", ".markdown", ".txt", ".rst", ".html", ".htm"}
MAX_DOC_CHARS = 400_000  # 单文档截断保护


class BaseReader(ABC):
    """Reader 基类。"""

    def __init__(self, bl: BusinessLine, source: SourceConfig) -> None:
        self.bl = bl
        self.source = source
        self.options: Dict[str, Any] = source.options or {}

    @abstractmethod
    def read(self) -> List[SourceDoc]:
        ...

    # ---- 公共助手 ----
    def _make_doc(self, title: str, content: str, uri: str, **meta: Any) -> SourceDoc:
        return SourceDoc(
            business_line=self.bl.id,
            title=title[:200],
            content=content[:MAX_DOC_CHARS],
            source_type=self.source.type,
            source_uri=uri,
            lang=self.bl.language,
            authority=self.source.authority,
            tags=list(self.source.tags),
            meta=meta,
            updated_at=utcnow(),
        )

    def _resolve(self, path: str) -> str:
        """相对路径按项目根目录解析。"""
        if os.path.isabs(path):
            return path
        root = self.options.get("root")
        if root:
            return os.path.join(root, path)
        return path


@REGISTRY.reader("markdown_dir")
class MarkdownDirReader(BaseReader):
    """递归读取目录下的 .md/.txt/.html 文件。"""

    def read(self) -> List[SourceDoc]:
        base = self._resolve(self.source.path)
        patterns = self.options.get("ext", list(TEXT_EXT))
        patterns = {e if e.startswith(".") else "." + e for e in patterns}
        docs: List[SourceDoc] = []
        if not os.path.isdir(base):
            log.warning("目录不存在，跳过: %s", base)
            return docs
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in sorted(filenames):
                if os.path.splitext(fn)[1].lower() not in patterns:
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    with open(full, "r", encoding="utf-8", errors="ignore") as f:
                        raw = f.read()
                except OSError as exc:
                    log.warning("读取失败 %s: %s", full, exc)
                    continue
                if len(raw.strip()) < 10:
                    continue
                title = _first_heading(raw) or os.path.splitext(fn)[0]
                rel = os.path.relpath(full, base).replace("\\", "/")
                docs.append(self._make_doc(
                    title, raw, rel,
                    file_path=full, file_name=fn, rel_path=rel,
                ))
        return docs


@REGISTRY.reader("file")
class FileReader(BaseReader):
    """读取单个文件（任意文本格式）。"""

    def read(self) -> List[SourceDoc]:
        path = self._resolve(self.source.path)
        if not os.path.isfile(path):
            log.warning("文件不存在，跳过: %s", path)
            return []
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read()
        title = _first_heading(raw) or os.path.splitext(os.path.basename(path))[0]
        return [self._make_doc(title, raw, os.path.basename(path), file_path=path)]


@REGISTRY.reader("text")
class TextReader(BaseReader):
    """直接把配置里的 content 字段作为内容（适合少量手写/结构化文本）。"""

    def read(self) -> List[SourceDoc]:
        content = self.options.get("content") or self.options.get("text") or ""
        title = self.options.get("title") or self.source.path or "内联文本"
        if not content.strip():
            return []
        return [self._make_doc(title, content, self.options.get("uri", "inline://text"))]


@REGISTRY.reader("csv")
class CsvReader(BaseReader):
    """读取 CSV —— 每行一条文档，列名通过 options 映射。"""

    def read(self) -> List[SourceDoc]:
        path = self._resolve(self.source.path)
        if not os.path.isfile(path):
            log.warning("文件不存在，跳过: %s", path)
            return []
        title_col = self.options.get("title_col", "title")
        content_col = self.options.get("content_col", "content")
        uri_col = self.options.get("uri_col")
        docs: List[SourceDoc] = []
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for i, row in enumerate(csv.DictReader(f)):
                title = (row.get(title_col) or f"行{i + 1}").strip()
                content = (row.get(content_col) or "").strip()
                if not content:
                    # 无正文时，把所有非空列拼成 "键: 值" 形式
                    content = "\n".join(f"{k}: {v}" for k, v in row.items() if v)
                if not content.strip():
                    continue
                uri = (row.get(uri_col) if uri_col else None) or f"{os.path.basename(path)}#row{i + 1}"
                docs.append(self._make_doc(title, content, uri, row_index=i + 1, row=row))
        return docs


@REGISTRY.reader("jsonl")
class JsonlReader(BaseReader):
    """读取 JSONL：每行一个 {title, content, uri} 对象。"""

    def read(self) -> List[SourceDoc]:
        path = self._resolve(self.source.path)
        if not os.path.isfile(path):
            log.warning("文件不存在，跳过: %s", path)
            return []
        docs: List[SourceDoc] = []
        with open(path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("第 %d 行不是合法 JSON，跳过", i + 1)
                    continue
                docs.append(self._make_doc(
                    obj.get("title") or f"记录{i + 1}",
                    obj.get("content") or obj.get("text") or "",
                    obj.get("uri") or f"{os.path.basename(path)}#{i + 1}",
                    **{k: v for k, v in obj.items() if k not in ("title", "content", "text", "uri")},
                ))
        return docs


@REGISTRY.reader("url")
class UrlReader(BaseReader):
    """抓取网页正文（去标签、去脚本样式）。"""

    def read(self) -> List[SourceDoc]:
        url = self.source.path
        timeout = int(self.options.get("timeout", 20))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": self.options.get(
                "user_agent", "GEOBot/1.0 (+https://example.com/bot)")})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
        except Exception as exc:
            log.warning("抓取失败 %s: %s", url, exc)
            return []
        text = _html_to_text(html)
        title = _html_title(html) or url
        return [self._make_doc(title, text, url, fetched_from=url)]


@REGISTRY.reader("api")
class ApiReader(BaseReader):
    """调用内部 HTTP API 拉取内容，用 JMESPath 风格的简单点路径取值。

    options:
        url:        接口地址
        headers:    请求头
        items_path: 列表字段路径，如 "data.list"（空则把响应本身视为列表）
        title_key / content_key / uri_key: 字段映射
    """

    def read(self) -> List[SourceDoc]:
        url = self.options.get("url") or self.source.path
        if not url:
            log.warning("api reader 缺少 url 配置")
            return []
        timeout = int(self.options.get("timeout", 30))
        req = urllib.request.Request(
            url,
            headers=self.options.get("headers", {"Accept": "application/json"}),
            method=self.options.get("method", "GET"),
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            log.warning("接口调用失败 %s: %s", url, exc)
            return []
        items = _dig(data, self.options.get("items_path", "")) if self.options.get("items_path") else data
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            log.warning("接口返回不是列表: %s", url)
            return []
        tk, ck, uk = (self.options.get("title_key", "title"),
                      self.options.get("content_key", "content"),
                      self.options.get("uri_key", "uri"))
        docs = []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            content = _dig(item, ck)
            if not content:
                content = "\n".join(f"{k}: {v}" for k, v in item.items() if v not in (None, "", []))
            docs.append(self._make_doc(str(_dig(item, tk) or f"记录{i + 1}"), str(content),
                                       str(_dig(item, uk) or f"{url}#{i + 1}"), raw=item))
        return docs


# ---------------------------------------------------------------- 编排

def run_ingest(bl: BusinessLine, store, root: str = "") -> List[SourceDoc]:
    """按业务线配置执行全部来源接入，并落库。"""
    docs: List[SourceDoc] = []
    for src in bl.sources:
        if not REGISTRY.has("reader", src.type):
            log.warning("[%s] 未知来源类型 %s，跳过", bl.id, src.type)
            continue
        if root and not os.path.isabs(src.path):
            # 允许配置里写相对项目根目录的路径
            src = SourceConfig(type=src.type, path=os.path.join(root, src.path),
                               tags=src.tags, authority=src.authority,
                               options={**src.options, "root": root})
        reader_cls = REGISTRY.get("reader", src.type)
        try:
            got = reader_cls(bl, src).read()
        except Exception as exc:
            log.error("[%s] 来源 %s 读取异常: %s", bl.id, src.path, exc)
            continue
        log.info("[%s] 来源 %s(%s) 读入 %d 篇", bl.id, src.path, src.type, len(got))
        docs.extend(got)
    if store and docs:
        store.save_documents(docs)
    return docs


# ---------------------------------------------------------------- 文本清洗

_TAG_RE = re.compile(r"<script.*?</script>|<style.*?</script>|<style.*?</style>|"
                     r"<nav.*?</nav>|<footer.*?</footer>", re.S | re.I)
_BR_RE = re.compile(r"<(br|/p|/div|/li|/h[1-6])[^>]*>", re.I)
_ANY_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\u00a0]+")
_NL = re.compile(r"\n{3,}")


def _html_to_text(html: str) -> str:
    s = _TAG_RE.sub("", html or "")
    s = _BR_RE.sub("\n", s)
    s = _ANY_TAG.sub("", s)
    s = html_unescape(s)
    s = _WS.sub(" ", s)
    return _NL.sub("\n\n", s).strip()


def html_unescape(s: str) -> str:
    import html as _html
    return _html.unescape(s)


def _html_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.S | re.I)
    return html_unescape(m.group(1)).strip() if m else ""


def _first_heading(text: str) -> str:
    m = re.search(r"^#{1,3}\s*(.+)$", text or "", re.M)
    if m:
        return m.group(1).strip()
    first = (text or "").strip().splitlines()
    return first[0].strip("# ").strip() if first else ""


def _dig(obj: Any, path: str) -> Any:
    """按 a.b.0.c 这样的点路径取值，取不到返回 None。"""
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur
