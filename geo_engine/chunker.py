"""语义分块 —— 把长文档切成「主题完整、可独立引用」的最小片段。

原则（对生成式检索至关重要）：
    1. 按标题层级切，保证每块有明确主题（heading_path）；
    2. 块太小会丢失上下文，太大则噪声高：以 token 上限切分并保留句间重叠；
    3. 表格、代码块、列表尽量整块保留，AI 引用时不会断章取义；
    4. 输出时把标题路径拼进正文前缀，让片段脱离原文档也能自解释。
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import List, Optional, Tuple

from .models import Chunk, SourceDoc, estimate_tokens, split_sentences
from .registry import REGISTRY

_HEADING = re.compile(r"^(#{1,6})\s*(.+?)\s*$", re.M)
_FENCE = re.compile(r"^```.*?^```", re.S | re.M)
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.M)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.M)


@REGISTRY.chunker("semantic")
class SemanticChunker:
    """默认分块器。"""

    def __init__(self, max_tokens: int = 420, min_tokens: int = 40,
                 overlap_sentences: int = 1, prefix_heading: bool = True) -> None:
        self.max_tokens = max_tokens
        self.min_tokens = min_tokens
        self.overlap_sentences = overlap_sentences
        self.prefix_heading = prefix_heading

    # ---------------------------------------------------------------- 入口
    def chunk(self, doc: SourceDoc) -> List[Chunk]:
        text = _normalize(doc.content)
        if not text.strip():
            return []
        sections = self._split_by_heading(text)
        chunks: List[Chunk] = []
        for path, body in sections:
            for piece in self._split_long(body):
                piece = piece.strip()
                if estimate_tokens(piece) < self.min_tokens and chunks:
                    # 过短片段合并到上一块，避免碎片化
                    prev = chunks[-1]
                    chunks[-1] = replace(
                        prev,
                        text=(prev.text + "\n" + piece).strip(),
                        tokens=estimate_tokens(prev.text + "\n" + piece),
                    )
                    continue
                if not piece:
                    continue
                display = (f"{' > '.join(path)}\n\n{piece}" if (self.prefix_heading and path) else piece)
                chunks.append(Chunk(
                    doc_id=doc.id,
                    business_line=doc.business_line,
                    heading_path=" > ".join(path),
                    text=display,
                    meta={
                        "source_uri": doc.source_uri,
                        "title": doc.title,
                        "authority": doc.authority,
                        "tags": doc.tags,
                    },
                ))
        return chunks

    # ---------------------------------------------------------------- 切分
    def _split_by_heading(self, text: str) -> List[Tuple[List[str], str]]:
        """返回 [(标题路径列表, 正文)]。"""
        out: List[Tuple[List[str], str]] = []
        stack: List[Tuple[int, str]] = []
        last_pos = 0
        matches = list(_HEADING.finditer(text))

        def flush(end: int) -> None:
            body = text[last_pos:end].strip()
            if body:
                out.append(([t for _, t in stack], body))

        if not matches:
            return [([], text)]

        # 标题前的前言
        pre = text[:matches[0].start()].strip()
        if pre:
            out.append(([], pre))

        for i, m in enumerate(matches):
            level = len(m.group(1))
            title = m.group(2).strip()
            flush(m.start())
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            last_pos = m.end()
        flush(len(text))
        return out

    def _split_long(self, body: str) -> List[str]:
        """在保护表格/代码块的前提下，按句子把超长正文切成多块。"""
        blocks = _split_protected(body)
        pieces: List[str] = []
        buf: List[str] = []
        buf_tokens = 0
        for kind, blk in blocks:
            if kind != "text":
                # 表格/代码块：容量够就并入当前块，否则单独成块
                t = estimate_tokens(blk)
                if buf and buf_tokens + t > self.max_tokens:
                    pieces.append("\n".join(buf))
                    buf, buf_tokens = [], 0
                buf.append(blk)
                buf_tokens += t
                continue
            for sent in _split_sentences(blk):
                t = estimate_tokens(sent)
                if t > self.max_tokens:
                    # 超长单句（无标点长段落），硬切
                    if buf:
                        pieces.append("\n".join(buf))
                        buf, buf_tokens = [], 0
                    pieces.extend(_hard_split(sent, self.max_tokens))
                    continue
                if buf_tokens + t > self.max_tokens and buf:
                    pieces.append("\n".join(buf))
                    buf = buf[-self.overlap_sentences:] if self.overlap_sentences else []
                    buf_tokens = sum(estimate_tokens(x) for x in buf)
                buf.append(sent)
                buf_tokens += t
        if buf:
            pieces.append("\n".join(buf))
        return [p for p in pieces if p.strip()]


# ---------------------------------------------------------------- 工具

def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def _split_protected(body: str) -> List[Tuple[str, str]]:
    """把正文切成 [('text'|'block', 内容)]，代码块/表格/列表作为整体保留。"""
    out: List[Tuple[str, str]] = []
    pos = 0
    for m in _FENCE.finditer(body):
        if m.start() > pos:
            out.extend(_split_tables(body[pos:m.start()]))
        out.append(("block", m.group(0)))
        pos = m.end()
    if pos < len(body):
        out.extend(_split_tables(body[pos:]))
    return [(k, v) for k, v in out if v.strip()]


def _split_tables(seg: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    pos = 0
    for m in re.finditer(r"(?:^\s*\|.*\|\s*$\n?){2,}", seg, re.M):
        if m.start() > pos:
            out.append(("text", seg[pos:m.start()]))
        out.append(("block", m.group(0)))
        pos = m.end()
    if pos < len(seg):
        out.append(("text", seg[pos:]))
    return out


def _split_sentences(text: str) -> List[str]:
    parts = split_sentences(text)
    merged: List[str] = []
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        # 列表项单独成句，避免多个要点被揉成一段
        if _BULLET.search(p) and "\n" in p:
            merged.extend(x.strip() for x in p.split("\n") if x.strip())
        else:
            merged.append(re.sub(r"\s*\n\s*", " ", p))
    return merged


def _hard_split(sent: str, max_tokens: int) -> List[str]:
    size = max(max_tokens * 3, 200)
    return [sent[i:i + size] for i in range(0, len(sent), size)]


def build_chunker(name: str = "semantic", **kwargs):
    cls = REGISTRY.get("chunker", name)
    return cls(**kwargs)
