# -*- coding: utf-8 -*-
"""检索层基类：HTTP 重试/退避/超时、本地缓存、Reference 数据结构。"""
from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Optional

import requests


@dataclass
class Reference:
    source_name: str
    url: str
    title: Optional[str] = None
    author: Optional[str] = None
    dynasty: Optional[str] = None
    content_lines: list = field(default_factory=list)
    raw_content: str = ""


class Cache:
    """极简磁盘缓存：避免重复抓取同一作者列表/诗篇，既省钱又降低被封风险。"""

    def __init__(self, cache_dir: str):
        self.dir = cache_dir
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, key: str) -> str:
        safe = key.replace("/", "_").replace("\\", "_").replace(":", "_")
        return os.path.join(self.dir, safe + ".json")

    def get(self, key: str):
        p = self._path(key)
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return None
        return None

    def set(self, key: str, value) -> None:
        try:
            with open(self._path(key), "w", encoding="utf-8") as f:
                json.dump(value, f, ensure_ascii=False)
        except Exception:
            pass


class HttpClient:
    """带显式超时、重试、指数退避+抖动、限流的 HTTP 客户端（C4）。"""

    def __init__(self, req: "object", cache: Cache, delay: float):
        self.req = req
        self.cache = cache
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
        })

    def fetch(self, url: str, use_cache: bool = True, cache_key: Optional[str] = None) -> Optional[str]:
        ck = cache_key or ("html:" + url)
        if use_cache:
            cached = self.cache.get(ck)
            if cached is not None:
                return cached.get("html")
        html = self._request_with_retry(url)
        if html is not None and use_cache:
            self.cache.set(ck, {"html": html})
        if self.delay > 0:
            time.sleep(self.delay)
        return html

    def _request_with_retry(self, url: str) -> Optional[str]:
        last_err = None
        for attempt in range(self.req.retries + 1):
            try:
                resp = self.session.get(url, timeout=self.req.timeout)
                if resp.status_code == 404:
                    return None  # 终端错误，不重试
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.text
            except Exception as e:  # 超时/连接错误/5xx → 可重试
                last_err = e
                if attempt < self.req.retries:
                    backoff = min(
                        self.req.backoff_max,
                        self.req.backoff_base * (2 ** attempt),
                    ) + random.uniform(0, 0.5)
                    time.sleep(backoff)
        return None  # 重试耗尽 → 降级为无参考（不静默成功，调用方会记录 error）
