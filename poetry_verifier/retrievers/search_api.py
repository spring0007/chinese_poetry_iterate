# -*- coding: utf-8 -*-
"""可选的通用联网搜索检索器（用于无作者 corpus：诗经/楚辞/四书五经等）。

需要用户自备搜索 API Key（环境变量 POETRY_VERIFY_SEARCH_API_KEY），因为古文岛站内搜索已登录墙，
无法用于“无作者 + 按标题”的定位。本实现只负责“找到古文岛诗篇 URL”，拿到 URL 后仍交给
GushiwenRetriever.get_poem 解析，保证解析逻辑单一、可维护。

支持的引擎：tavily（默认，POST JSON）| generic（GET ?q= + Bearer Token）。
Bing / SerpAPI 的真实参数形态请按官方文档替换 [待核实: 各引擎请求/响应字段]。
"""
from __future__ import annotations

import json
import re
from typing import Optional

import requests

from .base import Reference
from .gushiwen import GushiwenRetriever


class SearchApiRetriever:
    NAME = "联网搜索API(找古文岛URL)"

    def __init__(self, http, cfg, gushiwen: GushiwenRetriever):
        self.http = http
        self.cfg = cfg
        self.gw = gushiwen

    def _query(self, title: str, author: Optional[str]) -> Optional[str]:
        if not self.cfg.search_api_url or not self.cfg.search_api_key:
            return None
        q = f"{title} {author or ''} 古诗文 古文岛".strip()
        try:
            if self.cfg.search_api_engine == "tavily":
                resp = requests.post(
                    self.cfg.search_api_url,
                    json={"api_key": self.cfg.search_api_key, "query": q, "max_results": 5},
                    timeout=self.http.req.timeout,
                )
            else:  # generic
                resp = requests.get(
                    self.cfg.search_api_url,
                    params={"q": q},
                    headers={"Authorization": f"Bearer {self.cfg.search_api_key}"},
                    timeout=self.http.req.timeout,
                )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return None
        # 在结果文本里找古文岛 shiwenv 链接
        blob = json.dumps(data, ensure_ascii=False)
        m = re.search(r"https?://(?:www\.)?gushiwen\.cn/shiwenv_[a-f0-9]+\.aspx", blob)
        return m.group(0) if m else None

    def resolve(self, local_title: Optional[str], local_author: Optional[str],
                local_rhythmic: Optional[str] = None,
                local_content_lines: Optional[list] = None) -> Optional[Reference]:
        title = local_title or local_rhythmic
        if not title:
            return None
        url = self._query(title, local_author)
        if not url:
            return None
        sid = re.search(r"shiwenv_([a-f0-9]+)\.aspx", url)
        if not sid:
            return None
        poem = self.gw.get_poem(sid.group(1))
        if not poem:
            return None
        return Reference(
            source_name=f"{self.NAME} → {self.gw.NAME}",
            url=url,
            title=poem.get("title"),
            author=poem.get("author"),
            dynasty=poem.get("dynasty"),
            content_lines=poem.get("content_lines", []),
            raw_content=poem.get("raw_content", ""),
        )
