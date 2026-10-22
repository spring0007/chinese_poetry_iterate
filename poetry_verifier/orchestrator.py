# -*- coding: utf-8 -*-
"""编排层：有界自主循环、断点续跑、限流/重试、落盘与报告（程枢 Phase 2-4）。

循环边界（C5）：max_poems / max_time 双重上限；每首诗词的处理也是有限步
（定位→比对→决策→标注）。危险操作（覆盖原文件）默认关闭，需显式确认（C6）。
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

from . import schemas as S
from . import verifier as V
from .annotator import annotate_record, no_reference_block
from .config import Settings
from .llm import LLMJudge
from .retrievers.base import Cache, HttpClient
from .retrievers.gushiwen import GushiwenRetriever
from .retrievers.search_api import SearchApiRetriever


class Orchestrator:
    def __init__(self, settings: Settings):
        self.s = settings
        self.cache = Cache(settings.cache_dir)
        self.http = HttpClient(settings.request, self.cache, settings.request.delay)
        self.gw = GushiwenRetriever(self.http, settings.request.max_pages, settings.title_match_ratio)
        self.search = None
        if settings.retrieval.search_api_url and settings.retrieval.search_api_key:
            self.search = SearchApiRetriever(self.http, settings.retrieval, self.gw)
        self.judge = LLMJudge(settings)
        self.processed = set()
        self.touched_files = set()
        self.stats = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "files_scanned": 0,
            "poems_processed": 0,
            "by_status": Counter(),
            "by_field": Counter(),
            "by_source": Counter(),
            "conflicts": [],
            "applied": [],
            "mismatches": [],
            "errors": [],
        }

    # ---------------- 断点 ----------------
    def _ck_path(self):
        return os.path.join(self.s.out_dir, "progress.json")

    def _load_checkpoint(self):
        p = self._ck_path()
        if self.s.resume and os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    self.processed = set(json.load(f).get("processed", []))
            except Exception:
                self.processed = set()

    def _save_checkpoint(self):
        os.makedirs(self.s.out_dir, exist_ok=True)
        with open(self._ck_path(), "w", encoding="utf-8") as f:
            json.dump({"processed": list(self.processed)}, f, ensure_ascii=False)

    # ---------------- 发现 ----------------
    def discover_files(self):
        single = getattr(self.s, "single_file", None)
        if single:
            if os.path.isfile(single):
                yield single
            return
        for dirpath, dirnames, filenames in os.walk(self.s.root):
            dirnames[:] = [d for d in dirnames if d not in self.s.exclude_dirs]
            for fn in sorted(filenames):
                if not fn.endswith(".json"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, self.s.root).replace(os.sep, "/")
                top = rel.split("/")[0]
                if self.s.collections and top not in self.s.collections:
                    continue
                yield full

    # ---------------- 检索 ----------------
    def resolve_ref(self, record):
        ref = self.gw.resolve(record.title, record.author, record.rhythmic, record.content_lines)
        if ref is None and self.search is not None:
            ref = self.search.resolve(record.title, record.author, record.rhythmic, record.content_lines)
        return ref

    # ---------------- LLM 精炼 ----------------
    def _refine_with_llm(self, decision):
        for c in decision["changes"]:
            if c.get("needs_review") and not c.get("applied"):
                r = self.judge.judge(c["field"], c["old"], c["new"])
                if r:
                    accept, reason, conf = r
                    c["reason"] = reason
                    c["confidence"] = conf
                    if accept and conf >= self.s.verify.confidence_threshold:
                        c["applied"] = True
                        c["needs_review"] = False
        applied_any = any(c["applied"] for c in decision["changes"])
        review_any = any(c["needs_review"] and not c["applied"] for c in decision["changes"])
        if review_any:
            decision["status"] = "conflict_needs_review"
        elif applied_any:
            decision["status"] = "corrected"

    # ---------------- 单首处理 ----------------
    def process_record(self, record):
        try:
            ref = self.resolve_ref(record)
        except Exception as e:
            self.stats["errors"].append({"file": record.file_rel, "uid": record.uid, "err": str(e)})
            no_reference_block(record, f"检索异常：{e}")
            return
        if ref is None:
            no_reference_block(record, "未找到权威参考（来源不可达 / 无作者无法定位 / 标题未匹配）")
            decision = {"status": "no_reference", "changes": [], "notes": []}
        else:
            decision = V.decide(record, ref, self.s)
            if self.judge.enabled:
                self._refine_with_llm(decision)
            annotate_record(record, ref, decision)
        self._collect_stats(record, ref, decision)

    def _collect_stats(self, record, ref, decision):
        self.stats["poems_processed"] += 1
        st = decision["status"]
        self.stats["by_status"][st] += 1
        if ref:
            self.stats["by_source"][ref.source_name] += 1
        for c in decision.get("changes", []):
            if c.get("applied"):
                self.stats["by_field"][c["field"]] += 1
                self.stats["applied"].append({
                    "file": record.file_rel, "uid": record.uid,
                    "title": record.display_title(), "author": record.author,
                    "field": c["field"], "old": c["old"], "new": c["new"],
                    "reason": c["reason"], "confidence": c["confidence"],
                    "source": ref.source_name if ref else None,
                    "url": ref.url if ref else None,
                })
            if c.get("needs_review") and not c.get("applied"):
                self.stats["conflicts"].append({
                    "file": record.file_rel, "uid": record.uid,
                    "title": record.display_title(), "author": record.author,
                    "field": c["field"], "old": c["old"], "new": c["new"],
                    "reason": c["reason"], "confidence": c["confidence"],
                })
        # 参考错配（已跳过覆盖，防误改）
        if decision["status"] == "reference_mismatch":
            self.stats["mismatches"].append({
                "file": record.file_rel, "uid": record.uid,
                "title": record.display_title(), "author": record.author,
                "notes": decision.get("notes", []),
            })

    # ---------------- 主循环 ----------------
    def run(self):
        start = time.time()
        self._load_checkpoint()
        count = 0
        for path in self.discover_files():
            if self.s.max_poems and count >= self.s.max_poems:
                break
            if self.s.max_time and (time.time() - start) > self.s.max_time:
                break
            try:
                raw, rel, dyn = S.load_and_plan(path, self.s.root)
            except Exception as e:
                self.stats["errors"].append({"file": path, "err": f"解析失败：{e}"})
                continue
            self.stats["files_scanned"] += 1
            cls = S.classify_file(raw)
            if cls != "poem_collection":
                continue
            recs = list(S.iter_poem_records(raw, rel, dyn))
            file_changed = False
            for rec in recs:
                key = f"{rel}#{rec.uid}"
                if key in self.processed:
                    continue
                if self.s.max_poems and count >= self.s.max_poems:
                    break
                if self.s.max_time and (time.time() - start) > self.s.max_time:
                    break
                self.process_record(rec)
                self.processed.add(key)
                count += 1
                file_changed = True
                if count % 25 == 0:
                    self._save_checkpoint()
            if file_changed:
                self.touched_files.add(rel)
                self._write_file(path, rel, raw)
        self._save_checkpoint()
        self._write_report()
        self.stats["finished_at"] = datetime.now(timezone.utc).isoformat()
        self.stats["touched_files"] = len(self.touched_files)
        return self.stats

    # ---------------- 落盘 ----------------
    def _write_file(self, src_path, rel, raw):
        if self.s.in_place and (not self.s.require_confirm):
            target = src_path
        else:
            target = os.path.join(self.s.out_dir, rel)
        S.dump_json(raw, target)

    # ---------------- 报告 ----------------
    def _write_report(self):
        os.makedirs(self.s.out_dir, exist_ok=True)
        report = dict(self.stats)
        report["by_status"] = dict(self.stats["by_status"])
        report["by_field"] = dict(self.stats["by_field"])
        report["by_source"] = dict(self.stats["by_source"])
        with open(os.path.join(self.s.out_dir, "report.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        # 人类可读变更日志
        lines = ["# 诗词数据核对变更日志", ""]
        lines.append(f"- 处理诗词数：{self.stats['poems_processed']}")
        lines.append(f"- 状态分布：{dict(self.stats['by_status'])}")
        lines.append(f"- 自动修正字段：{dict(self.stats['by_field'])}")
        lines.append("")
        lines.append("## 已自动修正 / 补全")
        for c in self.stats["applied"][:1000]:
            old = c["old"]
            new = c["new"]
            old_s = " / ".join(old) if isinstance(old, list) else str(old)
            new_s = " / ".join(new) if isinstance(new, list) else str(new)
            lines.append(
                f"- [{c['file']}] 《{c['title']}》{c['author'] or ''} | 字段 `{c['field']}` | "
                f"置信度 {c['confidence']}\n  - 原值：{old_s}\n  - 新值：{new_s}\n"
                f"  - 来源：{c['source']} {c['url'] or ''}\n  - 说明：{c['reason']}"
            )
        lines.append("")
        lines.append("## 冲突（待人工复核）")
        for c in self.stats["conflicts"][:500]:
            lines.append(
                f"- [{c['file']}] 《{c['title']}》{c['author'] or ''} | 字段 `{c['field']}` | "
                f"置信度 {c['confidence']} | {c['reason']}"
            )
        lines.append("")
        lines.append("## 参考疑似错配（已跳过覆盖，待人工核对篇目）")
        if not self.stats["mismatches"]:
            lines.append("（无）")
        for m in self.stats["mismatches"][:500]:
            note = "；".join(m.get("notes", [])) or "参考疑似错配"
            lines.append(
                f"- [{m['file']}] 《{m['title']}》{m['author'] or ''} | {note}"
            )
        with open(os.path.join(self.s.out_dir, "changelog.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))


def healthcheck(settings: Settings) -> dict:
    cache = Cache(settings.cache_dir)
    http = HttpClient(settings.request, cache, 0)
    gw = GushiwenRetriever(http, 1, 0.82)
    ok, msg = gw.healthcheck()
    return {"gushiwen": {"reachable": ok, "detail": msg}}
