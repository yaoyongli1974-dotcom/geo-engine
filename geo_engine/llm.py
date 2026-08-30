"""LLM 抽象层 —— 可插拔模型 provider。

- HeuristicLLM：离线启发式实现，不联网、确定性，保证无 API Key 时全链路可跑通；
- OpenAICompatLLM：兼容 OpenAI /v1/chat/completions 协议（含 DeepSeek、通义、月之暗面、vLLM 等）。

新增 provider：继承 LLMProvider 并在 geo_engine.registry 注册即可。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from .models import LLMConfig, split_sentences as _split_sentences


class LLMError(RuntimeError):
    pass


class LLMProvider(ABC):
    """所有 provider 的统一接口。"""

    name = "base"

    @abstractmethod
    def complete(self, prompt: str, *, system: Optional[str] = None,
                 json_mode: bool = False, max_tokens: Optional[int] = None) -> str:
        """返回纯文本结果；json_mode=True 时要求返回合法 JSON 字符串。"""

    # 便捷：解析 JSON 输出，失败时抛出 LLMError
    def complete_json(self, prompt: str, *, system: Optional[str] = None) -> Any:
        raw = self.complete(prompt, system=system, json_mode=True)
        return extract_json(raw)


def extract_json(raw: str) -> Any:
    """从可能带 ```json 围栏或多余文本的输出中提取 JSON。"""
    if raw is None:
        raise LLMError("模型返回为空")
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 截取第一个完整 JSON 结构
    for opener, closer in (("{", "}"), ("[", "]")):
        s, e = text.find(opener), text.rfind(closer)
        if s != -1 and e > s:
            try:
                return json.loads(text[s:e + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"无法解析模型输出为 JSON: {raw[:200]}")


# ---------------------------------------------------------------- 启发式实现

class HeuristicLLM(LLMProvider):
    """离线启发式 provider。

    不调用任何外部服务，按提示词中的指令关键字走规则模板。
    目的是：没有 API Key 时，整条 GEO 流水线依然能产出一个可用的、结构规范的版本，
    用户后续只需把 provider 换成 openai_compat 即可得到 LLM 增强版。
    """

    name = "heuristic"

    def __init__(self, config: Optional[LLMConfig] = None, **_: Any) -> None:
        self.config = config or LLMConfig()

    def complete(self, prompt: str, *, system: Optional[str] = None,
                 json_mode: bool = False, max_tokens: Optional[int] = None) -> str:
        task = _detect_task(prompt)
        handler = getattr(self, f"_task_{task}", self._task_generic)
        result = handler(prompt)
        return json.dumps(result, ensure_ascii=False) if json_mode and not isinstance(result, str) else (
            result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        )

    # ---- 各类任务的规则实现 ----
    def _task_facts(self, prompt: str) -> List[Dict[str, Any]]:
        """从事实抽取提示中，按句子级规则产出事实卡。"""
        body = _extract_input(prompt)
        out: List[Dict[str, Any]] = []
        for sent in _split_sentences(body):
            if len(sent) < 8:
                continue
            nums = re.findall(r"(\d+(?:\.\d+)?)\s*(%|个百分点|米|m|米/秒|mm|毫米|kg|千克|吨|"
                              r"万元|元|亿|万|台|套|个|人|年|月|日|小时|天|次|度|kWh|W|kW|A|V|"
                              r"Mbps|Gbps|GHz|TB|GB|dB|lux|lx|℃)", sent)
            qualifies = bool(nums) or any(k in sent for k in
                                          ("应", "需", "必须", "建议", "采用", "符合", "达到",
                                           "支持", "可", "优于", "不得低于", "标准", "认证"))
            if not qualifies:
                continue
            # topic 留空：由调用方回退到分块标题，比规则猜测更准
            out.append({
                "claim": _tidy(sent),
                "topic": "",
                "numbers": [{"value": v, "unit": u} for v, u in nums[:4]],
                "entities": _guess_entities(sent),
                "confidence": min(0.95, 0.5 + 0.12 * len(nums) + (0.1 if "标准" in sent or "认证" in sent else 0)),
            })
            if len(out) >= 8:
                break
        return out

    def _task_qa(self, prompt: str) -> List[Dict[str, Any]]:
        body = _extract_input(prompt)
        out: List[Dict[str, Any]] = []
        headings = re.findall(r"^#{1,4}\s*(.+)$", body, re.M)
        for h in headings[:10]:
            h = h.strip()
            if len(h) < 3:
                continue
            out.append({
                "question": f"{h} 是什么？",
                "answer": _first_sentences_after(body, h) or f"{h}相关内容参见正文。",
                "intent": "informational",
                "topic": h,
            })
        for sent in _split_sentences(body)[:40]:
            if not out:
                break
            if any(k in sent for k in ("如何", "怎么", "多少", "为什么", "是否", "标准")):
                out.append({
                    "question": _to_question(sent),
                    "answer": _tidy(sent),
                    "intent": "informational",
                    "topic": _guess_topic(sent),
                })
            if len(out) >= 12:
                break
        return out[:12]

    def _task_terms(self, prompt: str) -> List[Dict[str, Any]]:
        body = _extract_input(prompt)
        out: List[Dict[str, Any]] = []
        # 「术语：解释」「术语 —— 解释」「**术语**：解释」三种常见写法
        patterns = [
            r"^[-*]?\s*\*\*?([^\n：:]{2,20})\*\*?\s*[：:]\s*(.+)$",
            r"^[-*]?\s*([^\n：:]{2,20})\s*[—–-]{1,2}\s*(.+)$",
        ]
        for pat in patterns:
            for m in re.finditer(pat, body, re.M):
                term, defin = m.group(1).strip(), m.group(2).strip()
                if len(defin) < 4:
                    continue
                out.append({"term": term, "definition": defin[:200], "aliases": [], "related": []})
            if out:
                break
        return out[:30]

    def _task_rewrite(self, prompt: str) -> str:
        body = _extract_input(prompt)
        sents = _split_sentences(body)
        return _tidy(sents[0]) if sents else _tidy(body[:120])

    def _task_generic(self, prompt: str) -> str:
        body = _extract_input(prompt)
        return _tidy(body[:200])


# ---------------------------------------------------------------- OpenAI 兼容实现

class OpenAICompatLLM(LLMProvider):
    """OpenAI /v1/chat/completions 兼容 provider（标准库 urllib 实现，无依赖）。"""

    name = "openai_compat"

    def __init__(self, config: Optional[LLMConfig] = None, **_: Any) -> None:
        self.config = config or LLMConfig()
        api_key = os.getenv(self.config.api_key_env, "")
        if not api_key:
            raise LLMError(
                f"缺少 API Key：请在环境变量 {self.config.api_key_env} 中配置，"
                f"或把业务线 llm.provider 改成 heuristic 使用离线模式。"
            )
        self.api_key = api_key

    def complete(self, prompt: str, *, system: Optional[str] = None,
                 json_mode: bool = False, max_tokens: Optional[int] = None) -> str:
        cfg = self.config
        url = cfg.base_url.rstrip("/") + "/chat/completions"
        payload: Dict[str, Any] = {
            "model": cfg.model,
            "temperature": cfg.temperature,
            "max_tokens": max_tokens or cfg.max_tokens,
            "messages": [
                {"role": "system", "content": system or "你是严谨的企业知识工程师，输出必须基于给定材料，不臆造。"},
                {"role": "user", "content": prompt},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise LLMError(f"LLM 请求失败 {exc.code}: {exc.read().decode('utf-8', 'ignore')[:300]}") from exc
        except Exception as exc:  # 网络/超时
            raise LLMError(f"LLM 请求异常: {exc}") from exc
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"LLM 返回结构异常: {str(data)[:300]}") from exc


# ---------------------------------------------------------------- 工厂

_PROVIDERS = {
    "heuristic": HeuristicLLM,
    "openai_compat": OpenAICompatLLM,
}


def build_llm(config: LLMConfig) -> LLMProvider:
    """按配置构造 provider；构造失败（如缺 Key）时给出明确提示。"""
    cls = _PROVIDERS.get(config.provider)
    if cls is None:
        raise LLMError(f"未知 LLM provider: {config.provider}；可选: {', '.join(_PROVIDERS)}")
    return cls(config)


def register_provider(name: str, cls) -> None:
    """允许外部注册自定义 provider（扩展点）。"""
    _PROVIDERS[name] = cls


# ---------------------------------------------------------------- 文本工具

def _extract_input(prompt: str) -> str:
    """从提示词中取出被 ``` 或 【材料】 包裹的正文。"""
    fence = re.search(r"```(?:text|txt|md|markdown)?\s*(.+?)```", prompt, re.S)
    if fence:
        return fence.group(1).strip()
    m = re.search(r"(?:材料|正文|内容|文本)[：:]\s*(.+)$", prompt, re.S)
    return (m.group(1) if m else prompt).strip()


def _tidy(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _guess_topic(sent: str) -> str:
    m = re.search(r"([\u4e00-\u9fffA-Za-z0-9]{2,12})(系统|方案|标准|规范|技术|产品|服务|指标|流程)", sent)
    return m.group(0) if m else ""


def _guess_entities(sent: str) -> List[str]:
    ents = re.findall(r"[A-Z][A-Za-z0-9\-\+]{2,15}|[\u4e00-\u9fff]{2,8}(?:系统|平台|标准|认证|协议|规范)", sent)
    seen, out = set(), []
    for e in ents:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out[:8]


def _first_sentences_after(body: str, heading: str, n: int = 2) -> str:
    m = re.search(r"^#{1,4}\s*" + re.escape(heading) + r"\s*$(.+?)(?=^#{1,4}\s|\Z)", body, re.M | re.S)
    if not m:
        return ""
    sents = _split_sentences(m.group(1))
    return " ".join(_tidy(s) for s in sents[:n])


def _to_question(sent: str) -> str:
    s = _tidy(sent).rstrip("。.")
    core = s[:28]
    if "多少" in core or "几" in core:
        return f"{core}？"
    if "如何" in core or "怎么" in core:
        return f"{core}？"
    return f"关于“{core}”，应如何理解？"


def _detect_task(prompt: str) -> str:
    p = prompt.lower()
    if any(k in prompt for k in ("事实卡", "可引用", "抽取事实", "fact")):
        return "facts"
    if any(k in prompt for k in ("问答对", "FAQ", "问题与答案", "qa")):
        return "qa"
    if any(k in prompt for k in ("术语", "词条", "glossary", "定义")):
        return "terms"
    if any(k in prompt for k in ("改写", "重写", "一句话", "压缩")):
        return "rewrite"
    return "generic"
