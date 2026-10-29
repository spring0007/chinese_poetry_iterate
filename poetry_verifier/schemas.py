# -*- coding: utf-8 -*-
"""数据契约层：识别诗词集合文件、抽取字段、并写回（仅在原对象上追加 _verify 字段）。

关键约束（用户要求：维持原有目录与字段结构）：
- 不删除/重命名任何原有字段。
- 所有修改痕迹只通过新增的 `_verify` 字段承载。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

# 顶层文集目录 -> 朝代（用于补全/核对 dynasty）
FOLDER_DYNASTY = {
    "全唐诗": "唐", "御定全唐詩": "唐", "水墨唐诗": "唐",
    "宋词": "宋",
    "诗经": "先秦", "楚辞": "先秦",
    "元曲": "元", "元诗": "元",
    "明": "明", "清诗": "清", "纳兰性德": "清",
    "汉": "汉", "魏晋": "魏晋", "南北朝": "南北朝", "隋": "隋",
    "金": "金", "五代诗词": "五代", "近现代": "近现代",
}


@dataclass
class PoemRecord:
    file_rel: str
    idx: int
    obj: dict                                  # 底层可变 dict 引用
    title: Optional[str] = None
    author: Optional[str] = None
    rhythmic: Optional[str] = None
    dynasty_derived: Optional[str] = None
    content_key: str = ""
    title_key: str = "title"
    author_key: str = "author"
    content_lines: list = field(default_factory=list)
    uid: str = ""

    @property
    def content_text(self) -> str:
        if isinstance(self.content_lines, str):
            return self.content_lines
        return "\n".join(self.content_lines or [])

    def display_title(self) -> str:
        return self.title or self.rhythmic or "(无题)"


def _is_poem_record(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    has_text = ("paragraphs" in obj) or ("content" in obj) or ("para" in obj)
    has_id = ("title" in obj) or ("author" in obj) or ("rhythmic" in obj) or ("chapter" in obj)
    return has_text and has_id


def _is_author_record(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    return ("name" in obj) and (("desc" in obj) or ("description" in obj)) \
        and not (("paragraphs" in obj) or ("content" in obj))


def classify_file(data: Any) -> str:
    """返回 'poem_collection' | 'author_collection' | 'skip'。"""
    if isinstance(data, list):
        if not data:
            return "skip"
        poem = sum(1 for x in data if _is_poem_record(x))
        auth = sum(1 for x in data if _is_author_record(x))
        if poem >= max(1, len(data) * 0.6):
            return "poem_collection"
        if auth >= max(1, len(data) * 0.6):
            return "author_collection"
        return "skip"
    if isinstance(data, dict):
        if _is_poem_record(data):
            return "poem_collection"
        return "skip"
    return "skip"


def _content_lines_of(obj: dict):
    if "paragraphs" in obj and isinstance(obj["paragraphs"], list):
        return "paragraphs", [str(x) for x in obj["paragraphs"]]
    if "content" in obj and isinstance(obj["content"], list):
        return "content", [str(x) for x in obj["content"]]
    if "para" in obj and isinstance(obj["para"], list):
        return "para", [str(x) for x in obj["para"]]
    if "paragraphs" in obj and isinstance(obj["paragraphs"], str):
        return "paragraphs", [obj["paragraphs"]]
    if "content" in obj and isinstance(obj["content"], str):
        return "content", [obj["content"]]
    if "para" in obj and isinstance(obj["para"], str):
        return "para", [obj["para"]]
    return "", []


def make_record(file_rel: str, obj: dict, idx: int, dynasty_derived: Optional[str]) -> PoemRecord:
    ckey, clines = _content_lines_of(obj)
    title = obj.get("title")
    author = obj.get("author")
    rhythmic = obj.get("rhythmic")
    uid = obj.get("id") or f"{file_rel}#{idx}"
    return PoemRecord(
        file_rel=file_rel, idx=idx, obj=obj,
        title=title, author=author, rhythmic=rhythmic,
        dynasty_derived=dynasty_derived,
        content_key=ckey, content_lines=clines, uid=str(uid),
    )


def iter_poem_records(raw: Any, file_rel: str, dynasty_derived: Optional[str]):
    """从已解析的 JSON 中产出 PoemRecord（持有底层对象引用，便于写回）。"""
    if isinstance(raw, list):
        for i, item in enumerate(raw):
            if _is_poem_record(item):
                yield make_record(file_rel, item, i, dynasty_derived)
    elif isinstance(raw, dict) and _is_poem_record(raw):
        yield make_record(file_rel, raw, -1, dynasty_derived)


def dynasty_from_folder(folder_name: str) -> Optional[str]:
    if folder_name in FOLDER_DYNASTY:
        return FOLDER_DYNASTY[folder_name]
    return None


def load_and_plan(path: str, root: str) -> tuple[Any, str, Optional[str]]:
    """加载文件并返回 (raw, relative_path, dynasty_from_folder)。"""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    rel = os.path.relpath(path, root)
    rel = rel.replace(os.sep, "/")
    top = rel.split("/")[0]
    return raw, rel, dynasty_from_folder(top)


def dump_json(obj: Any, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=False)
