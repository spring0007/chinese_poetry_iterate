# -*- coding: utf-8 -*-
"""可选的 LLM 裁判（OpenAI 兼容端点）。

作用：当启用时，对“冲突类”修改（标题/作者/正文与权威来源不一致）做智能裁决，
读取本地值与权威值，返回 {accept, reason, confidence}。未启用时，冲突默认进入“待人工复核”。

安全与合规（C1/C6）：
- 端点、模型、Key 全部由用户配置；Key 仅来自环境变量 POETRY_VERIFY_LLM_API_KEY，不硬编码。
- 模型真实名称、价格、上下文长度请以供应商文档为准 [待核实: 用户自备模型的名称/价格]。
- 调用失败不致命，自动回退到确定性策略（绝不静默成功）。
"""
from __future__ import annotations

import json
import time
from typing import Optional, Tuple

import requests


_PROMPT = """你是一位严谨的中国古典文献校对专家。下面给出某首诗词的“本地数据库字段值”与“权威来源（古文岛）字段值”。
请判断：对于该字段，是否应当用权威来源的值覆盖本地值。
判定原则：
1. 仅当权威来源明显更准确（如本地有错别字、脱字、标点混入正文、繁简用字差异导致语义不同）时才 accept=true；
2. 若两者只是版本异文、或无充分把握，accept=false（交给人工复核）；
3. 给出一句中文理由与 0~1 的置信度。

本地值：{local}
权威值：{ref}
字段：{field}

只输出 JSON，格式：{{"accept": true/false, "reason": "中文理由", "confidence": 0.0~1.0}}"""


class LLMJudge:
    def __init__(self, settings):
        self.s = settings.llm
        self.enabled = settings.llm.enabled and bool(settings.llm.api_key)
        # 用量累计（C5 成本可见性）：真实来自 API 返回的 usage
        self.calls = 0
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.last_usage = None

    def judge(self, field: str, old, new) -> Optional[Tuple[bool, str, float]]:
        if not self.enabled:
            return None
        url = self.s.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.s.model,
            "temperature": self.s.temperature,
            "messages": [
                {"role": "system", "content": "你是古典文献校对专家，只输出 JSON。"},
                {"role": "user", "content": _PROMPT.format(
                    local=_jsonable(old), ref=_jsonable(new), field=field)},
            ],
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.s.api_key}",
            "Content-Type": "application/json",
        }
        for attempt in range(2):
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=self.s.timeout)
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                obj = json.loads(_extract_json(content))
                accept = bool(obj.get("accept"))
                reason = str(obj.get("reason", ""))
                conf = float(obj.get("confidence", 0.5))
                # 捕获真实 token 用量
                u = data.get("usage") or {}
                self.calls += 1
                self.last_usage = u
                self.usage["prompt_tokens"] += int(u.get("prompt_tokens", 0) or 0)
                self.usage["completion_tokens"] += int(u.get("completion_tokens", 0) or 0)
                self.usage["total_tokens"] += int(u.get("total_tokens", 0) or 0)
                return accept, reason, conf
            except Exception:
                time.sleep(1.0)
        return None  # 回退到确定性策略

    def cost_estimate(self, price_per_1m_input=0.27, price_per_1m_output=1.10,
                      currency: str = "USD") -> dict:
        """按给定的每百万 token 单价估算费用（价格以供应商文档为准，属[待核实]项）。

        DeepSeek deepseek-chat 参考价（2025 标准档）：输入 $0.27/1M、输出 $1.10/1M；
        另有 prompt 缓存命中价更低。此处用标准档做上限估计。
        """
        inp = self.usage["prompt_tokens"]
        out = self.usage["completion_tokens"]
        cost = (inp / 1_000_000) * price_per_1m_input + (out / 1_000_000) * price_per_1m_output
        return {
            "currency": currency,
            "price_per_1m_input": price_per_1m_input,
            "price_per_1m_output": price_per_1m_output,
            "input_cost": round((inp / 1_000_000) * price_per_1m_input, 6),
            "output_cost": round((out / 1_000_000) * price_per_1m_output, 6),
            "total_cost": round(cost, 6),
        }


def _jsonable(v):
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _extract_json(s: str) -> str:
    s = s.strip()
    if s.startswith("{"):
        return s
    i = s.find("{")
    j = s.rfind("}")
    if i >= 0 and j > i:
        return s[i:j + 1]
    return s
