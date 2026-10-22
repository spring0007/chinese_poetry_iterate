# -*- coding: utf-8 -*-
"""配置层：所有可调参数集中在此，支持 JSON 文件 + 环境变量覆盖。

设计原则（程枢）：
- 凭据只来自环境变量或密钥服务，禁止硬编码（C6）。
- 所有外部调用的超时/重试/限流都在此声明（C4）。
- 自主循环的上限（max_poems / max_time）在此声明（C5）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class RequestSettings:
    timeout: float = 20.0          # 单次 HTTP 超时（秒），显式数值（C4）
    retries: int = 3               # 可重试次数（C4）
    backoff_base: float = 1.5      # 退避基数（秒），退避 = base * 2**attempt + 抖动（C4）
    backoff_max: float = 30.0      # 退避上限（秒）
    delay: float = 1.2             # 两次请求之间的礼貌性间隔（秒），限流（C4）
    max_pages: int = 8             # 作者诗列表最多翻几页（每页 ~30 首）；提高可扩大覆盖但增加抓取量


@dataclass
class RetrievalSettings:
    # 主检索源：古文岛（古诗文网同源，仅作单一权威参考，不能互相校对）
    primary: str = "gushiwen"
    # 可选：通用联网搜索 API（Tavily / Bing / SerpAPI 等），用于无作者 corpus（诗经/楚辞等）
    # 需要用户自备 key；未配置时，无作者的诗词标记为 no_reference（不臆造）。
    search_api_url: str = ""       # 例如 "https://api.tavily.com/search"
    search_api_key: str = ""       # 从环境变量 POETRY_VERIFY_SEARCH_API_KEY 读取
    search_api_engine: str = "tavily"  # tavily | bing | serpapi | generic


@dataclass
class VerifySettings:
    confidence_threshold: float = 0.80   # 低于此置信度的修改不自动写入（C5 有界）
    fill_placeholders: bool = True       # 是否补全 □ / [..] 等缺失/存疑字
    add_missing_fields: bool = False     # 是否把缺失的 dynasty 等字段真正写入（默认仅标注）
    overwrite_conflicts: bool = False    # 是否自动覆盖“冲突”字段（默认 False → 仅标注待人工复核）


@dataclass
class LLMSettings:
    enabled: bool = False
    # OpenAI 兼容端点，可换成任意供应商：
    #   DeepSeek      -> https://api.deepseek.com/v1          (model: deepseek-chat)  ← 默认，性价比首选
    #   通义千问 Qwen  -> https://dashscope.aliyuncs.com/compatible-mode/v1  (model: qwen-max / qwen-plus)
    #   智谱 GLM      -> https://open.bigmodel.cn/api/paas/v4 (model: glm-4-flash / glm-4-plus)
    #   OpenAI        -> https://api.openai.com/v1            (model: gpt-4o-mini / gpt-4o)
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""                             # 从环境变量 POETRY_VERIFY_LLM_API_KEY 读取
    model: str = "deepseek-chat"                  # 性价比首选：中文推理强、价格极低、JSON 稳定
    temperature: float = 0.0
    timeout: float = 30.0


@dataclass
class Settings:
    root: str = r"E:\chinese_poetry_iterate"
    out_dir: str = r"E:\chinese_poetry_iterate\verify_output"
    cache_dir: str = r"E:\chinese_poetry_iterate\verify_output\.cache"
    single_file: str = ""      # 仅处理单个文件（用于测试/精修）
    # 仅处理这些顶层文集目录；为空表示处理 root 下全部（排除 exclude_dirs）
    collections: list = field(default_factory=list)
    exclude_dirs: list = field(default_factory=lambda: [
        ".venv", "build", "poetry-pipeline", "node_modules", "_probe",
        "_scrape", ".git", ".idea", "__pycache__", "images", "strains",
        "rank", "loader", ".workbuddy",
    ])
    # 词牌/标题匹配的模糊阈值
    title_match_ratio: float = 0.82
    max_poems: int = 0            # 单次运行最多处理多少首（0=不限制，配合 max_time 使用）
    max_time: int = 0             # 墙钟上限（秒），0=不限制（C5）
    dry_run: bool = True          # 默认：只写 verify_output 副本，不动原文件
    in_place: bool = False        # 直接覆盖原文件（危险，需 --yes 二次确认，C6）
    require_confirm: bool = True  # in_place 时必须显式 --yes
    resume: bool = True           # 断点续跑
    request: RequestSettings = field(default_factory=RequestSettings)
    retrieval: RetrievalSettings = field(default_factory=RetrievalSettings)
    verify: VerifySettings = field(default_factory=VerifySettings)
    llm: LLMSettings = field(default_factory=LLMSettings)

    def to_dict(self) -> dict:
        d = asdict(self)
        # 不把密钥序列化进报告/配置
        d["retrieval"]["search_api_key"] = bool(d["retrieval"]["search_api_key"])
        d["llm"]["api_key"] = bool(d["llm"]["api_key"])
        return d


def load_settings(config_path: Optional[str] = None) -> Settings:
    """从 JSON 配置文件 + 环境变量加载设置；环境变量优先。"""
    s = Settings()
    if config_path and os.path.isfile(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        def pick(d, *keys, default=None):
            cur = d
            for k in keys:
                if isinstance(cur, dict) and k in cur:
                    cur = cur[k]
                else:
                    return default
            return cur

        if pick(data, "root"):
            s.root = data["root"]
        if pick(data, "out_dir"):
            s.out_dir = data["out_dir"]
        if pick(data, "collections"):
            s.collections = data["collections"]
        if pick(data, "exclude_dirs"):
            s.exclude_dirs = data["exclude_dirs"]
        if pick(data, "title_match_ratio"):
            s.title_match_ratio = data["title_match_ratio"]
        if pick(data, "max_poems"):
            s.max_poems = data["max_poems"]
        if pick(data, "max_time"):
            s.max_time = data["max_time"]
        if "dry_run" in data:
            s.dry_run = data["dry_run"]
        if "in_place" in data:
            s.in_place = data["in_place"]

        if pick(data, "request"):
            r = data["request"]
            s.request = RequestSettings(
                timeout=pick(r, "timeout", default=s.request.timeout),
                retries=pick(r, "retries", default=s.request.retries),
                backoff_base=pick(r, "backoff_base", default=s.request.backoff_base),
                backoff_max=pick(r, "backoff_max", default=s.request.backoff_max),
                delay=pick(r, "delay", default=s.request.delay),
                max_pages=pick(r, "max_pages", default=s.request.max_pages),  # noqa
            )
        if pick(data, "retrieval"):
            r = data["retrieval"]
            s.retrieval = RetrievalSettings(
                primary=pick(r, "primary", default=s.retrieval.primary),
                search_api_url=pick(r, "search_api_url", default=s.retrieval.search_api_url),
                search_api_key=pick(r, "search_api_key", default="") or os.environ.get("POETRY_VERIFY_SEARCH_API_KEY", ""),
                search_api_engine=pick(r, "search_api_engine", default=s.retrieval.search_api_engine),
            )
        if pick(data, "verify"):
            r = data["verify"]
            s.verify = VerifySettings(
                confidence_threshold=pick(r, "confidence_threshold", default=s.verify.confidence_threshold),
                fill_placeholders=pick(r, "fill_placeholders", default=s.verify.fill_placeholders),
                add_missing_fields=pick(r, "add_missing_fields", default=s.verify.add_missing_fields),
                overwrite_conflicts=pick(r, "overwrite_conflicts", default=s.verify.overwrite_conflicts),
            )
        if pick(data, "llm"):
            r = data["llm"]
            s.llm = LLMSettings(
                enabled=pick(r, "enabled", default=False),
                base_url=pick(r, "base_url", default=s.llm.base_url),
                api_key=pick(r, "api_key", default="") or os.environ.get("POETRY_VERIFY_LLM_API_KEY", ""),
                model=pick(r, "model", default=s.llm.model),
                temperature=pick(r, "temperature", default=s.llm.temperature),
                timeout=pick(r, "timeout", default=s.llm.timeout),
            )

    # 环境变量兜底（密钥只走环境变量，C6）
    if not s.retrieval.search_api_key:
        s.retrieval.search_api_key = os.environ.get("POETRY_VERIFY_SEARCH_API_KEY", "")
    if not s.llm.api_key:
        s.llm.api_key = os.environ.get("POETRY_VERIFY_LLM_API_KEY", "")
    return s
