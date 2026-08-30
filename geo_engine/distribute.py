"""自动化分发 —— 把构建产物持续、增量地推送到 AI 引擎可检索的渠道。

内置发布器：
    local_static  写入本地静态目录（可直接挂 Nginx / 对象存储同步）
    git           写入 Git 仓库并自动 commit/push（适配 GitHub Pages 等静态托管）
    http          POST 到自建接口 / 内部 CMS / Webhook
    indexnow      主动向 IndexNow 提交 URL（Bing/Copilot 等已接入该协议的引擎）
    noop          空实现，用于演练

扩展：继承 BasePublisher 并用 @REGISTRY.publisher("xxx") 注册，配置里写 type 即可。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from .logutil import get_logger
from .models import Artifact, BusinessLine, PublishResult, TargetConfig, utcnow
from .registry import REGISTRY

log = get_logger("distribute")


class BasePublisher(ABC):
    """发布器基类。"""

    def __init__(self, bl: BusinessLine, target: TargetConfig, root: str = "") -> None:
        self.bl = bl
        self.target = target
        self.root = root
        self.options: Dict[str, Any] = target.options or {}

    @abstractmethod
    def publish(self, artifacts: List[Artifact]) -> PublishResult:
        ...

    def abs_path(self, path: str) -> str:
        """相对路径按项目根目录解析。"""
        if not path or os.path.isabs(path):
            return path
        return os.path.join(self.root, path) if self.root else path

    def _ok(self, published: int, skipped: int = 0, message: str = "",
            **detail: Any) -> PublishResult:
        return PublishResult(
            target_id=self.target.id or self.target.type,
            target_type=self.target.type,
            ok=True, published=published, skipped=skipped, message=message, detail=detail,
        )

    def _fail(self, message: str, **detail: Any) -> PublishResult:
        return PublishResult(
            target_id=self.target.id or self.target.type,
            target_type=self.target.type,
            ok=False, message=message, detail=detail,
        )


@REGISTRY.publisher("local_static")
class LocalStaticPublisher(BasePublisher):
    """写入本地目录；可选清理旧文件（默认关闭，避免误删）。"""

    def publish(self, artifacts: List[Artifact]) -> PublishResult:
        out_dir = self.abs_path(self.options.get("dir") or "")
        if not out_dir:
            return self._fail("缺少 options.dir 配置")
        clean = bool(self.options.get("clean", False))
        if clean and os.path.isdir(out_dir):
            for name in os.listdir(out_dir):
                p = os.path.join(out_dir, name)
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        written = 0
        for a in artifacts:
            path = os.path.join(out_dir, a.path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(a.content)
            written += 1
        return self._ok(written, message=f"已写入 {out_dir}", dir=out_dir)


@REGISTRY.publisher("git")
class GitPublisher(BasePublisher):
    """写入 Git 仓库并自动提交推送。

    options:
        repo:      本地仓库路径（必填）
        remote:    远端名，默认 origin
        branch:    分支，默认 main
        push:      是否推送，默认 true
        commit_message: 提交信息模板，支持 {bl} {time} {count}
    """

    def publish(self, artifacts: List[Artifact]) -> PublishResult:
        repo = self.abs_path(self.options.get("repo") or "")
        if not repo or not os.path.isdir(repo):
            return self._fail(f"Git 仓库不存在: {repo}")
        subdir = self.options.get("subdir", "")
        base = os.path.join(repo, subdir) if subdir else repo
        for a in artifacts:
            path = os.path.join(base, a.path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(a.content)

        def git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                                  encoding="utf-8", errors="ignore")

        git("add", "-A")
        msg = (self.options.get("commit_message") or "chore(geo): 更新 {bl} 知识资产 {count} 个文件")
        msg = msg.format(bl=self.bl.id, count=len(artifacts), time=utcnow())
        commit = git("commit", "-m", msg)
        pushed = False
        push_msg = "未推送（配置为不推送或无变更）"
        if self.options.get("push", True):
            if "nothing to commit" not in (commit.stdout or ""):
                remote = self.options.get("remote", "origin")
                branch = self.options.get("branch", "main")
                pr = git("push", remote, branch)
                pushed = pr.returncode == 0
                push_msg = (pr.stderr or pr.stdout or "").strip()[:200]
        return self._ok(len(artifacts), message=push_msg, repo=repo, pushed=pushed,
                        commit=commit.stdout.strip()[:200])


@REGISTRY.publisher("http")
class HttpPublisher(BasePublisher):
    """POST 到 HTTP 接口。

    options:
        url:       接口地址（必填）
        headers:   请求头（可含鉴权，建议从环境变量注入）
        method:    POST / PUT，默认 POST
        batch:     单批文件数，默认 50
        payload:   json（默认）| multipart（暂未实现，回退 json）
    """

    def publish(self, artifacts: List[Artifact]) -> PublishResult:
        url = self.options.get("url")
        if not url:
            return self._fail("缺少 options.url 配置")
        method = self.options.get("method", "POST").upper()
        batch = int(self.options.get("batch", 50))
        headers = {"Content-Type": "application/json"}
        for k, v in (self.options.get("headers") or {}).items():
            # 支持 "Bearer ${ENV_NAME}" 形式从环境变量取值，避免密钥入库
            if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
                import os as _os
                v = _os.getenv(v[2:-1], "")
            headers[k] = v
        sent, errors = 0, []
        for i in range(0, len(artifacts), batch):
            chunk = artifacts[i:i + batch]
            payload = {
                "business_line": self.bl.id,
                "updated_at": utcnow(),
                "files": [{"path": a.path, "content": a.content, "format": a.format,
                           "checksum": a.checksum} for a in chunk],
            }
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req,
                                            timeout=int(self.options.get("timeout", 60))) as resp:
                    if resp.status >= 400:
                        errors.append(f"批次 {i // batch + 1}: HTTP {resp.status}")
                    else:
                        sent += len(chunk)
            except urllib.error.HTTPError as exc:
                errors.append(f"批次 {i // batch + 1}: {exc.code} {exc.read().decode('utf-8','ignore')[:120]}")
            except Exception as exc:
                errors.append(f"批次 {i // batch + 1}: {exc}")
        if errors:
            return self._fail("；".join(errors[:3]), sent=sent)
        return self._ok(sent, message=f"已推送 {sent} 个文件到 {url}", url=url)


@REGISTRY.publisher("indexnow")
class IndexNowPublisher(BasePublisher):
    """向 IndexNow 提交变更 URL，加速被接入该协议的引擎（Bing/Copilot 等）收录。

    options:
        key:           IndexNow key（也支持 ${ENV} 写法）
        key_location:  key 文件对应的 URL（需放在站点根目录）
        endpoint:      默认 https://api.indexnow.org/indexnow
        host:          站点主机名，默认取 authority.website 的 host
    """

    def publish(self, artifacts: List[Artifact]) -> PublishResult:
        base_url = (self.bl.authority.website or self.bl.domain or "").rstrip("/")
        if not base_url:
            return self._fail("未配置 authority.website，无法生成 IndexNow 提交地址")
        key = self.options.get("key", "")
        if isinstance(key, str) and key.startswith("${") and key.endswith("}"):
            key = os.getenv(key[2:-1], "")
        if not key:
            return self._fail("缺少 IndexNow key（options.key 或环境变量）")
        host = self.options.get("host") or _host_of(base_url)
        urls = [f"{base_url}/{a.path.lstrip('/')}" for a in artifacts
                if a.path.endswith((".html", ".md", ".txt"))]
        if not urls:
            return self._ok(0, message="无可提交的 URL")
        payload = {
            "host": host,
            "key": key,
            "keyLocation": self.options.get("key_location") or f"{base_url}/{key}.txt",
            "urlList": urls[: int(self.options.get("max_urls", 1000))],
        }
        endpoint = self.options.get("endpoint", "https://api.indexnow.org/indexnow")
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=int(self.options.get("timeout", 30))) as resp:
                code = resp.status
        except urllib.error.HTTPError as exc:
            # IndexNow 对 200/202 均视为成功，其余记录错误
            if exc.code in (200, 202):
                code = exc.code
            else:
                return self._fail(f"IndexNow 提交失败: {exc.code}")
        except Exception as exc:
            return self._fail(f"IndexNow 提交异常: {exc}")
        return self._ok(len(urls), message=f"IndexNow 已提交 {len(urls)} 个 URL（HTTP {code}）",
                        endpoint=endpoint)


@REGISTRY.publisher("noop")
class NoopPublisher(BasePublisher):
    """演练用：只统计不落盘。"""

    def publish(self, artifacts: List[Artifact]) -> PublishResult:
        return self._ok(len(artifacts), message="noop：未实际发布",
                        paths=[a.path for a in artifacts][:20])


# ---------------------------------------------------------------- 编排

class DistributionManager:
    """分发编排：增量判定 → 逐个目标发布 → 记录产物指纹。"""

    def __init__(self, bl: BusinessLine, store=None, root: str = "") -> None:
        self.bl = bl
        self.store = store
        self.root = root

    def diff(self, artifacts: List[Artifact], force: bool = False) -> List[Artifact]:
        """与上次发布记录比对，只产出真正变化的文件。"""
        if force or self.store is None:
            return artifacts
        known = self.store.artifact_checksums(self.bl.id)
        return [a for a in artifacts if known.get(a.path) != a.checksum]

    def run(self, artifacts: List[Artifact], force: bool = False,
            only: Optional[List[str]] = None) -> List[PublishResult]:
        changed = self.diff(artifacts, force)
        results: List[PublishResult] = []
        if not changed:
            log.info("[%s] 内容无变化，跳过发布", self.bl.id)
            return results
        for target in self.bl.targets:
            if not target.enabled:
                continue
            if only and target.type not in only and (target.id or "") not in only:
                continue
            if not REGISTRY.has("publisher", target.type):
                results.append(PublishResult(target_id=target.id, target_type=target.type,
                                             ok=False, message=f"未注册的发布器: {target.type}"))
                continue
            publisher = REGISTRY.get("publisher", target.type)(self.bl, target, root=self.root)
            try:
                res = publisher.publish(changed)
            except Exception as exc:
                res = PublishResult(target_id=target.id, target_type=target.type,
                                    ok=False, message=f"发布异常: {exc}")
            log.info("[%s] 目标 %s(%s): %s — %s", self.bl.id, target.id or "-", target.type,
                     "成功" if res.ok else "失败", res.message[:120])
            results.append(res)
        if self.store is not None and any(r.ok for r in results):
            self.store.mark_artifacts(self.bl.id, [a.to_dict() for a in changed])
        return results


# ---------------------------------------------------------------- 调度

class Scheduler:
    """轻量调度器：按固定间隔循环执行任务（不引入 APScheduler 等依赖）。

    生产环境更推荐用系统 cron / CI 定时触发 CLI，这里提供 crontab 行生成。
    """

    def __init__(self, interval_hours: float = 24) -> None:
        self.interval_hours = interval_hours

    def run_forever(self, job, max_runs: int = 0) -> None:
        """阻塞式循环；max_runs>0 时执行指定次数后退出（便于测试）。"""
        interval = max(self.interval_hours, 0.01) * 3600
        n = 0
        while True:
            started = time.time()
            try:
                job()
            except Exception as exc:  # 单次失败不中断调度
                log.error("定时任务执行失败: %s", exc)
            n += 1
            if max_runs and n >= max_runs:
                break
            elapsed = time.time() - started
            sleep_for = max(interval - elapsed, 0)
            log.info("下次执行：%.1f 小时后", sleep_for / 3600)
            time.sleep(sleep_for)

    @staticmethod
    def crontab_line(python_bin: str, project_root: str, bl_id: str,
                     hour: int = 3, minute: int = 0) -> str:
        """生成可直接粘进 crontab 的一行（每天固定时间跑一次全量）。"""
        cmd = f'{python_bin} -m geo_engine.cli --root "{project_root}" run --bl {bl_id}'
        return f"{minute} {hour} * * * {cmd} >> {project_root}/logs/{bl_id}.log 2>&1"


def _host_of(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url if "//" in url else "https://" + url).hostname or ""
