# -*- coding: utf-8 -*-
"""标注与落盘层：在原对象上追加 _verify 字段（保留全部原有字段），并写入已批准的修改。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .schemas import PoemRecord
from .retrievers.base import Reference


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def annotate_record(record: PoemRecord, ref: Optional[Reference], decision: dict) -> dict:
    """就地修改 record.obj：应用已批准的修改，并追加 _verify 标注。"""
    for c in decision.get("changes", []):
        if not c.get("applied"):
            continue
        f = c["field"]
        if f in ("paragraphs", "content"):
            record.obj[f] = c["new"]
        elif f == "dynasty":
            record.obj["dynasty"] = c["new"]
        elif f in ("title", "author"):
            # 仅在原字段缺失或明确批准覆盖时写入
            record.obj[f] = c["new"]
        else:
            record.obj[f] = c["new"]

    block = {
        "checked_at": _now(),
        "source": ({"name": ref.source_name, "url": ref.url} if ref else None),
        "reference": (
            {
                "title": ref.title,
                "author": ref.author,
                "dynasty": ref.dynasty,
                "content": ref.content_lines,
            } if ref else None
        ),
        "status": decision["status"],
        "changes": decision.get("changes", []),
        "notes": decision.get("notes", []),
    }
    record.obj["_verify"] = block
    return block


def no_reference_block(record: PoemRecord, reason: str) -> dict:
    block = {
        "checked_at": _now(),
        "source": None,
        "reference": None,
        "status": "no_reference",
        "changes": [],
        "notes": [reason],
    }
    record.obj["_verify"] = block
    return block
