# -*- coding: utf-8 -*-
"""人工复核通道（辅助裁判，仅作参考，绝不自动落盘）。

用途：主裁判（默认 deepseek-chat）跑完后会留下一批「待人工复核」的冲突。
本通道对这些冲突再调用一个辅助模型（默认 glm-4-flash，免费额度友好），
给出独立的「第二意见」（accept/reason/confidence），供人工决策参考。

设计（C6 安全）：
- 只读取 report.json 的 conflicts，向辅助模型提问；结果写入独立的 review_opinions.json / .md。
- 绝不修改任何诗词文件，绝不把辅助模型的 accept 直接写入（applied 仍以主裁判为准）。
- 辅助模型若调用失败，该项标记为 api_error，不影响其它项。

用法：
  python -m poetry_verifier.review_channel \
      --report verify_output/report.json \
      --out review_opinions.json \
      [--model glm-4-flash] [--base https://open.bigmodel.cn/api/paas/v4]
Key 从环境变量 POETRY_VERIFY_LLM_API_KEY 读取（不落盘）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .config import LLMSettings, load_settings
from .llm import LLMJudge
from . import normalize as N


def norm(v):
    if isinstance(v, list):
        return tuple(str(x) for x in v)
    return str(v)


def load_conflicts(report_path: str):
    d = json.load(open(report_path, encoding="utf-8"))
    seen = {}
    for c in d.get("conflicts", []):
        key = (c.get("uid"), c.get("field"), norm(c.get("old")), norm(c.get("new")))
        if key not in seen:
            seen[key] = c
    return list(seen.values())


def build_judge(args, settings) -> LLMJudge:
    api_key = os.environ.get("POETRY_VERIFY_LLM_API_KEY", "")
    llm = LLMSettings(
        enabled=bool(api_key),
        model=args.model or "glm-4-flash",
        base_url=args.base or "https://open.bigmodel.cn/api/paas/v4",
        api_key=api_key,
        temperature=0.0,
        timeout=30.0,
    )
    return LLMJudge(SimpleNamespace(llm=llm))


def main(argv=None):
    p = argparse.ArgumentParser(description="人工复核通道：对主裁判冲突调用辅助模型给第二意见")
    p.add_argument("--report", required=True, help="主裁判产出的 report.json 路径")
    p.add_argument("--out", required=True, help="输出 JSON 路径（第二意见）")
    p.add_argument("--model", help="辅助模型名（默认 glm-4-flash）")
    p.add_argument("--base", help="OpenAI 兼容端点 base_url")
    p.add_argument("--delay", type=float, default=0.5, help="每条请求间礼貌间隔（秒）")
    p.add_argument("--limit", type=int, default=0, help="最多处理多少条冲突（0=全部）")
    args = p.parse_args(argv)

    conflicts = load_conflicts(args.report)
    if args.limit:
        conflicts = conflicts[: args.limit]
    print(f"读取冲突: {len(conflicts)} 条")

    judge = build_judge(args, load_settings())
    if not judge.enabled:
        print("[错误] 未检测到 POETRY_VERIFY_LLM_API_KEY，无法调用辅助模型。请先设置环境变量。")
        return 2

    items = []
    advise_accept = advise_reject = api_error = 0
    for i, c in enumerate(conflicts, 1):
        field = c.get("field")
        old = c.get("old")
        new = c.get("new")
        r = judge.judge(field, old, new)
        if r is None:
            api_error += 1
            op = {"accept": None, "reason": "API 调用失败（已跳过）", "confidence": None}
        else:
            accept, reason, conf = r
            op = {"accept": bool(accept), "reason": reason, "confidence": conf}
            if accept:
                advise_accept += 1
            else:
                advise_reject += 1
        items.append({
            "uid": c.get("uid"),
            "title": c.get("title"),
            "author": c.get("author"),
            "field": field,
            "old": old,
            "new": new,
            "primary_reason": c.get("reason"),
            "primary_confidence": c.get("confidence"),
            "review": op,
        })
        if i % 10 == 0 or i == len(conflicts):
            print(f"  进度 {i}/{len(conflicts)}  建议接受 {advise_accept} / 建议复核 {advise_reject} / 失败 {api_error}")
        if args.delay:
            time.sleep(args.delay)

    out = {
        "review_model": judge.s.model,
        "review_base": judge.s.base_url,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_report": os.path.abspath(args.report),
        "summary": {
            "total": len(items),
            "advise_accept": advise_accept,
            "advise_reject": advise_reject,
            "api_error": api_error,
            "judge_calls": judge.calls,
            "usage": judge.usage,
        },
        "items": items,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # 人类可读复核单
    md = os.path.splitext(args.out)[0] + ".md"
    lines = ["# 人工复核单（辅助模型第二意见）", ""]
    lines.append(f"- 辅助模型：{judge.s.model}（端点 {judge.s.base_url}）")
    lines.append(f"- 来源报告：{os.path.abspath(args.report)}")
    lines.append(f"- 冲突总数：{len(items)} ｜ 建议接受 {advise_accept} ｜ 建议复核 {advise_reject} ｜ API 失败 {api_error}")
    lines.append("")
    lines.append("## 逐条")
    for it in items:
        rv = it["review"]
        acc = "接受覆盖" if rv["accept"] is True else ("复核" if rv["accept"] is False else "API失败")
        old_s = "/".join(it["old"]) if isinstance(it["old"], list) else str(it["old"] or "")
        new_s = "/".join(it["new"]) if isinstance(it["new"], list) else str(it["new"] or "")
        lines.append(
            f"- 《{it['title']}》{it['author'] or ''} · `{it['field']}` ｜ 辅助建议：**{acc}**"
            f"（conf={rv['confidence']}）：{rv['reason']}"
        )
        lines.append(f"  - 本地：{old_s[:60]} → 权威：{new_s[:60]}")
        lines.append(f"  - 主裁判：{it['primary_reason']}（conf={it['primary_confidence']}）")
    with open(md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\n完成：{args.out}")
    print(f"建议接受 {advise_accept} / 建议复核 {advise_reject} / API 失败 {api_error}")
    print(f"辅助模型调用 {judge.calls} 次，token {judge.usage['total_tokens']}")
    print(f"复核单：{md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
