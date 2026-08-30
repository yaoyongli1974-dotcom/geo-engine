"""格式化层公共工具：URL 规范化、HTML 转义、页面骨架。"""

from __future__ import annotations

import html
import json
from typing import Any, Dict, List
from urllib.parse import quote

from ..models import BusinessLine, Artifact, utcnow


def esc(text: Any) -> str:
    """HTML 转义（属性与正文通用）。"""
    return html.escape(str(text if text is not None else ""), quote=True)


def base_url_of(bl: BusinessLine) -> str:
    """取业务线的站点根 URL。"""
    raw = (bl.authority.website or bl.domain or "").strip()
    if not raw:
        return "https://example.com"
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw.rstrip("/")


def url_join(base: str, path: str) -> str:
    return f"{base}/{path.lstrip('/')}"


def safe_url_path(path: str) -> str:
    """把本地相对路径转成 URL 路径（保留中文，做百分号编码）。"""
    return "/".join(quote(p, safe="") for p in path.replace("\\", "/").split("/") if p)


def jsonld_script(data: Any) -> str:
    """生成 <script type="application/ld+json"> 片段。"""
    payload = json.dumps(data, ensure_ascii=False, indent=None)
    # 防止 </script> 提前闭合
    payload = payload.replace("</", "<\\/")
    return f'<script type="application/ld+json">{payload}</script>'


PAGE_CSS = """
:root{--bg:#ffffff;--fg:#1b1f24;--muted:#5b6570;--line:#e3e8ef;--accent:#0f7b6c;--soft:#f6f8fa}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
 line-height:1.75;font-size:16px}
.wrap{max-width:900px;margin:0 auto;padding:32px 20px 80px}
header{border-bottom:1px solid var(--line);padding-bottom:20px;margin-bottom:28px}
h1{font-size:28px;margin:0 0 8px}
h2{font-size:21px;margin:36px 0 12px;padding-left:10px;border-left:4px solid var(--accent)}
h3{font-size:17px;margin:22px 0 8px}
p.sub{color:var(--muted);margin:0}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.card{border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:14px 0;background:var(--soft)}
.card h3{margin-top:0}
.meta{color:var(--muted);font-size:13px;margin-top:10px}
.badge{display:inline-block;background:var(--accent);color:#fff;border-radius:4px;
 padding:1px 8px;font-size:12px;margin-right:6px}
blockquote{margin:10px 0;padding:10px 14px;border-left:3px solid var(--accent);
 background:var(--soft);color:#243b53}
table{border-collapse:collapse;width:100%;margin:12px 0}
th,td{border:1px solid var(--line);padding:8px 10px;text-align:left;font-size:15px}
th{background:var(--soft)}
nav.toc{background:var(--soft);border:1px solid var(--line);border-radius:10px;padding:14px 18px}
nav.toc ul{margin:6px 0 0 0;padding-left:20px}
footer{margin-top:48px;padding-top:18px;border-top:1px solid var(--line);color:var(--muted);font-size:13px}
code{background:var(--soft);padding:2px 5px;border-radius:4px;font-size:14px}
"""


def page(title: str, body: str, *, description: str = "", extra_head: str = "",
         bl: BusinessLine = None, canonical: str = "") -> str:
    """统一页面骨架：语义化标签 + 结构化数据挂载点。"""
    desc = description or (bl.description if bl else "")
    head_extra = extra_head
    if canonical:
        head_extra += f'\n<link rel="canonical" href="{esc(canonical)}">'
    return f"""<!DOCTYPE html>
<html lang="{esc((bl.language if bl else 'zh-CN') or 'zh-CN')}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
<meta name="author" content="{esc(bl.authority.org_legal_name if bl else '')}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:type" content="article">
<style>{PAGE_CSS}</style>{head_extra}
</head>
<body>
<div class="wrap">
{body}
</div>
</body>
</html>
"""


def artifact(path: str, content: str, fmt: str, bl: BusinessLine) -> Artifact:
    return Artifact(
        path=path.replace("\\", "/"),
        content=content,
        format=fmt,
        business_line=bl.id,
        updated_at=utcnow(),
    )
