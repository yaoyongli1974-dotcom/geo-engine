"""命令行入口。

常用命令::

    python -m geo_engine.cli --root . list
    python -m geo_engine.cli --root . run --bl demo --stage ingest --stage structure
    python -m geo_engine.cli --root . run --bl demo            # 跑通全链路
    python -m geo_engine.cli --root . run --all                # 所有业务线
    python -m geo_engine.cli --root . monitor --bl demo        # 只做一次监测
    python -m geo_engine.cli --root . schedule --bl demo --hours 24
    python -m geo_engine.cli --root . crontab --bl demo
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from . import __version__
from .config import ConfigRepository, dump_config, load_settings
from .distribute import Scheduler
from .logutil import get_logger
from .models import (
    AuthorityConfig,
    BusinessLine,
    LLMConfig,
    MonitorConfig,
    SourceConfig,
    TargetConfig,
)
from .pipeline import ALL_STAGES, GeoPipeline
from .registry import REGISTRY
from .store import Store

log = get_logger("cli")


# ---------------------------------------------------------------- 命令实现

def cmd_init(args: argparse.Namespace) -> int:
    """初始化项目骨架与示例业务线。"""
    settings = load_settings(args.root)
    settings.ensure_dirs()
    repo = ConfigRepository(settings)
    bl = BusinessLine(
        id=args.bl,
        name=args.name or args.bl,
        description=f"{args.name or args.bl} 的 GEO 知识资产",
        domain=args.domain or "example.com",
        topics=["主题一", "主题二"],
        audience=["采购决策者", "技术负责人"],
        authority=AuthorityConfig(
            org_legal_name=args.name or args.bl,
            website=args.domain or "example.com",
            industry=args.industry or "",
        ),
        sources=[SourceConfig(type="markdown_dir", path=f"content/{args.bl}", authority=3)],
        targets=[TargetConfig(id="local", type="local_static",
                              options={"dir": f"dist/{args.bl}"})],
        llm=LLMConfig(provider=args.llm),
        monitor=MonitorConfig(engines=["local"], queries=["示例问题？"],
                              competitors=[], interval_hours=24),
    )
    path = repo.save(bl)
    os.makedirs(settings.content_dir(bl.id), exist_ok=True)
    os.makedirs(settings.dist_dir(bl.id), exist_ok=True)
    os.makedirs(settings.report_dir(bl.id), exist_ok=True)
    print(f"[OK] 已初始化项目：{settings.root}")
    print(f"[OK] 业务线配置：{path}")
    print(f"[OK] 内容目录：{settings.content_dir(bl.id)}  ← 把企业资料(.md/.txt)放这里")
    print(f"[OK] 发布目录：{settings.dist_dir(bl.id)}")
    print("[提示] 修改 business_lines/*.json 后执行 run 即可。")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    settings = load_settings(args.root)
    repo = ConfigRepository(settings)
    bls = repo.load_all()
    if not bls:
        print("(暂无业务线，先执行 init)")
        return 0
    print("%-20s%-24s%8s%8s" % ("ID", "名称", "来源数", "目标数"))
    print("-" * 62)
    for bl in bls:
        print(f"{bl.id:<20}{(bl.name or '-'):<24}{len(bl.sources):>6}{len(bl.targets):>8}")
    return 0


def _run(args: argparse.Namespace, stages: Optional[List[str]]) -> int:
    settings = load_settings(args.root)
    store = Store(settings.db_path)
    pipe = GeoPipeline(settings, store)
    if args.all:
        results = pipe.run_all(stages, force=args.force, use_llm=not args.no_llm)
        code = 0
        for r in results:
            _print_result(r)
            code = max(code, 0 if r.ok() else 1)
        return code
    if not args.bl:
        print("错误：需要 --bl <业务线ID> 或 --all", file=sys.stderr)
        return 2
    res = pipe.run(args.bl, stages, force=args.force, use_llm=not args.no_llm)
    _print_result(res)
    return 0 if res.ok() else 1


def _print_result(res) -> None:
    print(f"\n===== 业务线 {res.bl_id} =====")
    for stage, info in res.stages.items():
        print(f"  [{stage}] {json.dumps(info, ensure_ascii=False)[:300]}")
    if res.errors:
        print("  [错误]")
        for e in res.errors:
            print("   -", e)
    print(f"  结果：{'成功' if res.ok() else '存在失败阶段'}")


def cmd_stats(args: argparse.Namespace) -> int:
    settings = load_settings(args.root)
    store = Store(settings.db_path)
    ids = [args.bl] if args.bl else ConfigRepository(settings).list_ids()
    for bl_id in ids:
        st = store.stats(bl_id)
        print(f"\n[{bl_id}]")
        for k, v in st.items():
            print(f"  {k:<12}{v}")
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    settings = load_settings(args.root)
    store = Store(settings.db_path)
    pipe = GeoPipeline(settings, store)
    intervals = {}
    try:
        intervals = {bl.id: bl.monitor.interval_hours for bl in ConfigRepository(settings).load_all()}
    except Exception:
        pass
    hours = args.hours or intervals.get(args.bl, 24)
    print(f"启动定时调度：业务线 {args.bl or 'ALL'}，间隔 {hours} 小时（Ctrl+C 退出）")

    def job() -> None:
        if args.bl:
            _print_result(pipe.run(args.bl, list(ALL_STAGES), force=False))
        else:
            for r in pipe.run_all(list(ALL_STAGES), force=False):
                _print_result(r)

    Scheduler(hours).run_forever(job, max_runs=args.max_runs)
    return 0


def cmd_crontab(args: argparse.Namespace) -> int:
    settings = load_settings(args.root)
    py = sys.executable
    bls = [args.bl] if args.bl else ConfigRepository(settings).list_ids()
    for bl_id in bls:
        print(Scheduler.crontab_line(py, settings.root, bl_id,
                                     hour=args.hour, minute=args.minute))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """环境自检：打印已注册组件与关键路径状态。"""
    settings = load_settings(args.root)
    print(f"GEO 引擎 v{__version__}   Python {sys.version.split()[0]}")
    print(f"项目根目录：{settings.root}")
    print("\n已注册组件：")
    for cat, names in REGISTRY.all().items():
        print(f"  {cat:<10}{', '.join(names) or '(空)'}")
    print("\n目录检查：")
    for label, p in (("业务线配置", settings.bl_dir()),
                     ("数据文件", settings.db_path),
                     ("报表输出", settings.path(settings.layout["reports"]))):
        print(f"  [{'存在' if os.path.exists(p) else '缺失'}] {label}: {p}")
    repo = ConfigRepository(settings)
    ids = repo.list_ids()
    print(f"\n业务线：{', '.join(ids) or '(无)'}")
    if ids:
        store = Store(settings.db_path)
        for i in ids:
            print(f"  {i}: {store.stats(i)}")
    print("\n结论：环境正常。" if ids else "\n提示：先执行 init 创建业务线。")
    return 0


# ---------------------------------------------------------------- 参数解析

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="geo_engine", description="GEO 生成式引擎优化引擎")
    p.add_argument("--root", default=".", help="项目根目录（默认当前目录）")
    p.add_argument("--version", action="version", version=f"geo_engine {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="初始化项目与业务线配置")
    sp.add_argument("--bl", default="demo", help="业务线 ID")
    sp.add_argument("--name", default="", help="业务线名称")
    sp.add_argument("--domain", default="", help="主域名")
    sp.add_argument("--industry", default="", help="行业")
    sp.add_argument("--llm", default="heuristic", choices=["heuristic", "openai_compat"])
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("list", help="列出业务线")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("check", help="环境与组件自检")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("stats", help="查看各业务线资产规模")
    sp.add_argument("--bl", default="")
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("run", help="执行流水线（默认全阶段）")
    sp.add_argument("--bl", default="")
    sp.add_argument("--all", action="store_true", help="所有业务线")
    sp.add_argument("--stage", action="append", choices=list(ALL_STAGES),
                    help="指定阶段，可重复；不指定则跑全部")
    sp.add_argument("--force", action="store_true", help="忽略增量判定，强制全量发布")
    sp.add_argument("--no-llm", action="store_true", help="禁用 LLM，纯离线规则模式")
    sp.set_defaults(func=lambda a: _run(a, a.stage))

    for name, help_text in (("ingest", "仅接入内容"), ("structure", "仅结构化整理"),
                            ("enhance", "仅语义增强"), ("build", "仅构建站点产物"),
                            ("publish", "仅分发"), ("monitor", "仅效果监测"),
                            ("report", "仅生成报表")):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--bl", required=True)
        sp.add_argument("--force", action="store_true")
        sp.add_argument("--no-llm", action="store_true")
        sp.set_defaults(func=lambda a, n=name: _run(a, [n]))

    sp = sub.add_parser("schedule", help="按间隔循环执行全链路")
    sp.add_argument("--bl", default="")
    sp.add_argument("--hours", type=float, default=0)
    sp.add_argument("--max-runs", type=int, default=0, help="执行次数上限，0=不限（测试用）")
    sp.set_defaults(func=cmd_schedule)

    sp = sub.add_parser("crontab", help="输出可直接使用的 crontab 行")
    sp.add_argument("--bl", default="")
    sp.add_argument("--hour", type=int, default=3)
    sp.add_argument("--minute", type=int, default=0)
    sp.set_defaults(func=cmd_crontab)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except Exception as exc:
        log.error("执行失败：%s", exc)
        if os.getenv("GEO_DEBUG"):
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
