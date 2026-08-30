"""AI 能力层 —— 用户自配置大模型调用 + 联网搜索整合。

职责：
  - 基于用户个人设置（provider / base_url / model / api_key）调用 OpenAI 兼容 Chat API；
  - 提供「内容完善 / 内容生成 / 联网搜索整合」三类能力；
  - 联网搜索支持两种路径：①模型自带联网（Perplexity、OpenAI web_search 工具）；
    ②显式搜索 API（Tavily / Brave）+ 模型整合；
  - 统一错误封装（AIError），便于上层返回清晰的错误信息。

密钥在调用前由 store 解密传入，本模块不负责存储。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .crypto import decrypt_secret

# ---------------------------------------------------------------- 异常
class AIError(RuntimeError):
    pass


# ---------------------------------------------------------------- 供应商预设
# web: 该供应商是否「自带联网」
#   - "perplexity": 调用 chat/completions 即返回联网答案（响应含 citations）
#   - "responses" : 走 OpenAI /v1/responses + web_search_preview 工具
#   - None        : 不自带联网，需显式搜索 API
PRESETS: Dict[str, Dict[str, str]] = {
    "openai":     {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "web": "responses"},
    "deepseek":   {"label": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "web": None},
    "moonshot":   {"label": "Kimi (Moonshot)", "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k", "web": None},
    "ollama":     {"label": "Ollama (本地)", "base_url": "http://localhost:11434/v1", "model": "llama3", "web": None},
    "perplexity": {"label": "Perplexity (自带联网)", "base_url": "https://api.perplexity.ai", "model": "sonar", "web": "perplexity"},
    "custom":     {"label": "自定义 OpenAI 兼容", "base_url": "", "model": "", "web": None},
}


def preset_list() -> List[Dict[str, str]]:
    return [{"id": k, "label": v["label"], "web": v["web"] or "none"}
            for k, v in PRESETS.items()]


# ---------------------------------------------------------------- 设置读取
def load_settings(store) -> Optional[Dict[str, Any]]:
    """从租户库读取设置并解密密钥；未配置返回 None。"""
    row = store.get_ai_settings()
    if not row:
        return None
    s = dict(row)
    s["api_key"] = decrypt_secret(s.get("api_key_enc") or "")
    s["search_key"] = decrypt_secret(s.get("search_key_enc") or "")
    try:
        s["extra"] = json.loads(s["extra"]) if s.get("extra") else {}
    except (ValueError, TypeError):
        s["extra"] = {}
    return s


def _resolve(settings: Dict[str, Any]) -> Tuple[str, str, str]:
    """返回 (base_url, model, api_key)，custom 用用户填写，其余用预设补全。"""
    provider = settings.get("provider") or "custom"
    preset = PRESETS.get(provider, {})
    base_url = (settings.get("base_url") or preset.get("base_url") or "").strip()
    model = (settings.get("model") or preset.get("model") or "").strip()
    api_key = settings.get("api_key") or ""
    if not base_url:
        raise AIError("缺少 API Base URL（自定义供应商需填写）")
    if not model:
        raise AIError("缺少模型名称（model）")
    return base_url, model, api_key


# ---------------------------------------------------------------- 客户端
class LLMClient:
    def __init__(self, settings: Dict[str, Any], timeout: float = 60.0) -> None:
        self.base_url, self.model, self.api_key = _resolve(settings)
        self.timeout = timeout
        self._http = httpx.Client(timeout=timeout)

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def chat(self, messages: List[Dict[str, str]], *, temperature: float = 0.7,
             max_tokens: int = 1500) -> str:
        """返回模型文本；异常统一封装为 AIError。"""
        url = self.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        try:
            r = self._http.post(url, headers=self._headers(), json=payload)
        except httpx.TimeoutException:
            raise AIError("模型请求超时（请检查网络或模型响应速度）")
        except httpx.HTTPError as exc:
            raise AIError(f"网络错误：{exc}")
        if r.status_code != 200:
            raise AIError(f"模型返回 {r.status_code}：{r.text[:300]}")
        try:
            data = r.json()
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise AIError(f"模型返回结构异常：{r.text[:200]}") from exc

    def chat_raw(self, messages: List[Dict[str, str]], *, max_tokens: int = 1500) -> Dict[str, Any]:
        """同 chat，但返回完整 JSON（用于读取 citations 等供应商扩展字段）。"""
        url = self.base_url.rstrip("/") + "/chat/completions"
        payload = {"model": self.model, "temperature": 0.3, "max_tokens": max_tokens, "messages": messages}
        try:
            r = self._http.post(url, headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            raise AIError(f"网络错误：{exc}")
        if r.status_code != 200:
            raise AIError(f"模型返回 {r.status_code}：{r.text[:300]}")
        try:
            return r.json()
        except ValueError as exc:
            raise AIError("模型返回非 JSON") from exc

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 校验
def validate_key(settings: Dict[str, Any]) -> Tuple[bool, str]:
    """用一次最小调用验证密钥可用性。"""
    try:
        c = LLMClient(settings, timeout=20)
        c.chat([{"role": "user", "content": "ping"}], max_tokens=4)
        c.close()
        return True, "连接成功，密钥有效"
    except AIError as e:
        return False, str(e)
    except Exception as e:  # pragma: no cover
        return False, f"校验异常：{e}"


# ---------------------------------------------------------------- 联网搜索
def search_web(settings: Dict[str, Any], query: str, max_results: int = 6) -> List[Dict[str, str]]:
    """显式搜索 API（Tavily / Brave）。模型自带联网不在此处理。"""
    sp = settings.get("search_provider") or "none"
    key = settings.get("search_key") or ""
    if sp == "tavily":
        if not key:
            raise AIError("未配置 Tavily API Key")
        try:
            r = httpx.post(
                "https://api.tavily.com/search",
                json={"query": query, "max_results": max_results, "search_depth": "basic"},
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise AIError(f"Tavily 网络错误：{exc}")
        if r.status_code != 200:
            raise AIError(f"Tavily 返回 {r.status_code}：{r.text[:200]}")
        data = r.json()
        return [{"title": x.get("title", ""), "url": x.get("url", ""),
                 "snippet": (x.get("content") or "")[:400]} for x in data.get("results", [])]
    if sp == "brave":
        if not key:
            raise AIError("未配置 Brave API Key")
        try:
            r = httpx.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": max_results},
                headers={"X-Subscription-Token": key, "Accept": "application/json"},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise AIError(f"Brave 网络错误：{exc}")
        if r.status_code != 200:
            raise AIError(f"Brave 返回 {r.status_code}：{r.text[:200]}")
        data = r.json()
        out = []
        for x in (data.get("web") or {}).get("results", []):
            out.append({"title": x.get("title", ""), "url": x.get("url", ""),
                        "snippet": (x.get("description") or "")[:400]})
        return out
    return []


def _openai_web_search(settings: Dict[str, Any], query: str) -> Dict[str, Any]:
    """OpenAI /v1/responses + web_search_preview 工具。"""
    base_url, model, api_key = _resolve(settings)
    url = base_url.rstrip("/") + "/responses"
    payload = {"model": model or "gpt-4o", "input": query,
               "tools": [{"type": "web_search_preview"}]}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        r = httpx.post(url, headers=headers, json=payload, timeout=90)
    except httpx.HTTPError as exc:
        raise AIError(f"OpenAI 联网请求失败：{exc}")
    if r.status_code != 200:
        raise AIError(f"OpenAI 返回 {r.status_code}：{r.text[:300]}")
    data = r.json()
    texts, urls = [], []
    for item in data.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    texts.append(c.get("text", ""))
                    for ann in c.get("annotations", []):
                        if ann.get("type") == "url_citation" and ann.get("url"):
                            urls.append(ann["url"])
    return {"text": "\n".join(texts),
            "sources": [{"title": "来源", "url": u, "snippet": ""} for u in urls]}


# ---------------------------------------------------------------- 三类能力
def ai_complete(settings: Dict[str, Any], text: str, instruction: str = "",
                context: str = "") -> str:
    """完善 / 续写 / 改写给定内容。"""
    sys = ("你是企业内容打磨助手。基于用户提供的素材，按指令进行完善、续写或润色，"
           "保持专业、准确、可被 AI 引擎引用；仅输出最终内容（Markdown），不要解释。")
    if context:
        sys += f"\n业务背景：{context}"
    user = f"指令：{instruction or '在不改变原意前提下，润色并补全内容，提升专业度与可读性'}\n\n素材：\n{text}"
    c = LLMClient(settings)
    try:
        return c.chat([{"role": "system", "content": sys}, {"role": "user", "content": user}],
                      temperature=0.6, max_tokens=2000)
    finally:
        c.close()


def ai_generate(settings: Dict[str, Any], topic: str, context: str = "",
                tone: str = "专业严谨", length: str = "中等") -> str:
    """根据主题生成一篇结构化内容草稿。"""
    sys = ("你是企业 GEO 内容创作助手。根据主题生成一篇结构清晰、可被生成式引擎引用的中文内容"
           "（Markdown，含小标题与要点），事实性内容需标注来源占位 [来源 n]。仅输出内容，不要解释。")
    if context:
        sys += f"\n业务背景：{context}"
    user = f"主题：{topic}\n风格：{tone}\n篇幅：{length}"
    c = LLMClient(settings)
    try:
        return c.chat([{"role": "system", "content": sys}, {"role": "user", "content": user}],
                      temperature=0.8, max_tokens=2200)
    finally:
        c.close()


def ai_research(settings: Dict[str, Any], query: str, context: str = "") -> Dict[str, Any]:
    """联网搜索并整合为调研内容。返回 {text, sources}。"""
    provider = settings.get("provider") or "custom"
    preset = PRESETS.get(provider, {})
    web = preset.get("web")
    if web == "perplexity":
        c = LLMClient(settings, timeout=90)
        try:
            sys = ("你是基于网络的调研助手。针对用户问题，结合联网检索给出准确、有出处的中文回答；"
                   "在正文以 [1] 等形式标注引用，并在末尾列出参考链接。")
            data = c.chat_raw([{"role": "system", "content": sys},
                               {"role": "user", "content": query}], max_tokens=2200)
            text = data["choices"][0]["message"]["content"]
            cites = data.get("citations") or []
            sources = [{"title": f"来源 {i+1}", "url": u, "snippet": ""}
                       for i, u in enumerate(cites)]
            return {"text": text, "sources": sources}
        finally:
            c.close()
    if web == "responses":
        return _openai_web_search(settings, query)
    # 显式搜索 API + 模型整合
    sp = settings.get("search_provider") or "none"
    if sp in ("tavily", "brave"):
        results = search_web(settings, query)
        if not results:
            raise AIError("搜索无结果，请调整查询或检查搜索 API Key")
        c = LLMClient(settings, timeout=90)
        try:
            snippets = "\n\n".join(
                f"[来源 {i+1}] {r['title']}\n{r['url']}\n{r['snippet']}"
                for i, r in enumerate(results)
            )
            prompt = (f"你是企业内容调研助手。基于下列检索结果，撰写一份结构化的中文调研报告（Markdown），"
                      f"覆盖用户问题「{query}」，严谨标注引用 [来源 n]，禁止臆造结果之外的信息。"
                      + (f"\n业务背景：{context}" if context else "") +
                      f"\n\n检索结果：\n{snippets}")
            text = c.chat([{"role": "user", "content": prompt}], temperature=0.4, max_tokens=2400)
            return {"text": text, "sources": results}
        finally:
            c.close()
    raise AIError(
        "未配置联网搜索：请选择支持联网的模型（Perplexity / OpenAI 联网），"
        "或在 AI 配置中填写 Tavily / Brave 搜索 API Key"
    )
