# -*- coding: utf-8 -*-
"""命令行入口。

典型用法：
  # 1) 先自检来源可达性
  python -m poetry_verifier.cli --health

  # 2) 竖切片预览（默认只写 verify_output 副本，不动原文件）
  python -m poetry_verifier.cli --file "全唐诗/error/poet.tang.0.json" --limit 20

  # 3) 对某文集全量预览
  python -m poetry_verifier.cli --collections 宋词 --limit 200

  # 4) 人工复核 changelog.md / report.json 后，确认覆盖原文件
  python -m poetry_verifier.cli --collections 宋词 --in-place --yes
"""
from __future__ import annotations

import argparse
import os
import sys

# 让 `python -m poetry_verifier.cli` 能找到同包模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .config import load_settings  # noqa: E402
from .orchestrator import Orchestrator, healthcheck  # noqa: E402


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="poetry_verifier",
        description="核对中国古典诗词 JSON 数据并据权威来源修正/补全（维持原结构，追加 _verify 标注）。",
    )
    p.add_argument("--config", help="JSON 配置文件路径")
    p.add_argument("--root", help="数据根目录")
    p.add_argument("--out", help="输出目录（预览副本）")
    p.add_argument("--file", help="仅处理单个 JSON 文件（相对 root 或绝对路径）")
    p.add_argument("--collections", help="仅处理这些顶层文集（逗号分隔），如 宋词,全唐诗")
    p.add_argument("--limit", type=int, default=0, help="本次最多处理多少首诗词（0=不限，配合 --max-time）")
    p.add_argument("--max-time", type=int, default=0, help="墙钟上限（秒）")
    p.add_argument("--title-ratio", type=float, help="标题匹配模糊阈值")
    p.add_argument("--delay", type=float, help="请求间礼貌间隔（秒）")
    p.add_argument("--max-pages", type=int, help="作者诗列表最多翻页数")
    p.add_argument("--confidence", type=float, help="自动写入的最低置信度")
    p.add_argument("--no-fill", action="store_true", help="关闭缺失/存疑字补全")
    p.add_argument("--add-fields", action="store_true", help="允许把缺失字段（如朝代）真正写入")
    p.add_argument("--overwrite", action="store_true", help="允许自动覆盖“冲突”字段（默认仅标注复核）")
    p.add_argument("--llm", action="store_true", help="启用 LLM 裁判（需配置 API Key）")
    p.add_argument("--llm-model", help="LLM 模型名")
    p.add_argument("--llm-base", help="OpenAI 兼容端点 base_url")
    p.add_argument("--search-url", help="联网搜索 API 地址（用于无作者 corpus）")
    p.add_argument("--search-engine", help="tavily | generic")
    p.add_argument("--in-place", action="store_true", help="直接覆盖原文件（危险，需 --yes）")
    p.add_argument("--yes", action="store_true", help="确认 in-place 覆盖")
    p.add_argument("--no-resume", action="store_true", help="忽略断点，从头跑")
    p.add_argument("--health", action="store_true", help="仅做来源可达性自检")
    return p


def main(argv=None):
    args = build_argparser().parse_args(argv)
    s = load_settings(args.config)

    # 命令行覆盖
    if args.root:
        s.root = args.root
    if args.out:
        s.out_dir = args.out
        s.cache_dir = os.path.join(s.out_dir, ".cache")
    if args.file:
        # 相对当前工作目录解析，避免与 root 叠加导致路径错误
        s.single_file = os.path.abspath(args.file)
    if args.collections:
        s.collections = [c.strip() for c in args.collections.split(",") if c.strip()]
    if args.limit:
        s.max_poems = args.limit
    if args.max_time:
        s.max_time = args.max_time
    if args.title_ratio is not None:
        s.title_match_ratio = args.title_ratio
    if args.delay is not None:
        s.request.delay = args.delay
    if args.max_pages is not None:
        s.request.max_pages = args.max_pages
    if args.confidence is not None:
        s.verify.confidence_threshold = args.confidence
    if args.no_fill:
        s.verify.fill_placeholders = False
    if args.add_fields:
        s.verify.add_missing_fields = True
    if args.overwrite:
        s.verify.overwrite_conflicts = True
    if args.llm:
        s.llm.enabled = True
    if args.llm_model:
        s.llm.model = args.llm_model
    if args.llm_base:
        s.llm.base_url = args.llm_base
    if args.search_url:
        s.retrieval.search_api_url = args.search_url
    if args.search_engine:
        s.retrieval.search_api_engine = args.search_engine
    if args.in_place:
        s.in_place = True
    if args.yes:
        s.require_confirm = False
    if args.no_resume:
        s.resume = False

    # 安全检查（C6）
    if s.in_place and s.require_confirm:
        print("[安全] 直接覆盖原文件已禁用。如需覆盖，请同时传递 --in-place --yes 并确认已备份。")
        print("本次改为输出到预览目录：", s.out_dir)
        s.in_place = False

    if args.health:
        hc = healthcheck(s)
        print("来源可达性：", hc)
        return 0

    print(f"根目录: {s.root}")
    print(f"输出目录: {s.out_dir}  (in_place={s.in_place})")
    print(f"LLM 裁判: {'启用' if s.llm.enabled and s.llm.api_key else '未启用（冲突将进入待复核）'}")
    print(f"补全缺失字: {s.verify.fill_placeholders}  写入缺失字段: {s.verify.add_missing_fields}  "
          f"覆盖冲突: {s.verify.overwrite_conflicts}")

    orch = Orchestrator(s)
    stats = orch.run()

    print("\n==== 运行结果 ====")
    print(f"扫描文件: {stats['files_scanned']}  处理诗词: {stats['poems_processed']}  "
          f"落盘文件: {stats['touched_files']}")
    print(f"状态分布: {dict(stats['by_status'])}")
    print(f"自动修正字段: {dict(stats['by_field'])}")
    if stats["by_source"]:
        print(f"参考来源: {dict(stats['by_source'])}")
    print(f"待人工复核(冲突): {len(stats['conflicts'])}")
    if stats["errors"]:
        print(f"错误: {len(stats['errors'])}（详见 {os.path.join(s.out_dir, 'report.json')}）")
    print(f"\n报告: {os.path.join(s.out_dir, 'report.json')}")
    print(f"变更日志: {os.path.join(s.out_dir, 'changelog.md')}")

    # LLM 裁判用量与费用（仅启用时）
    j = orch.judge
    if j.enabled and j.calls:
        cost = j.cost_estimate()
        print("\n==== LLM 裁判用量 / 费用（DeepSeek 标准档估算） ====")
        print(f"调用次数: {j.calls}")
        print(f"Token 用量: prompt={j.usage['prompt_tokens']}  completion={j.usage['completion_tokens']}  total={j.usage['total_tokens']}")
        if j.calls:
            print(f"单次平均: prompt≈{j.usage['prompt_tokens']//j.calls}  completion≈{j.usage['completion_tokens']//j.calls}  total≈{j.usage['total_tokens']//j.calls}")
        print(f"估算费用: {cost['total_cost']} {cost['currency']} "
              f"(输入 {cost['input_cost']} + 输出 {cost['output_cost']})  [单价输入${cost['price_per_1m_input']}/1M 输出${cost['price_per_1m_output']}/1M，以供应商文档为准]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
