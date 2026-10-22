# -*- coding: utf-8 -*-
"""仅把核对报告里的 safe 字段修正写回源文件，不添加任何 _verify 标注。

用法：
  python apply_safe_corrections.py --report <report.json> [--root <数据根>] [--dry-run]

匹配：report.applied[].uid = "<文件相对路径>#<数组下标>"（idx 为该词在文件数组中的位置）。
只改写确有差异的字段（title/dynasty/author/paragraphs/para/content），绝不写 `_verify`。
所有写回的文本新值一律简体化（语料整体已简体化，防繁体污染）。

写入方式：外科手术式文本补丁。各源文件序列化风格不尽相同且多含手工编辑痕迹
（前导空行、行内短数组、逗号后空格不一致、结尾空行等），整体重序列化会产生
整文件 diff 噪音。本工具按 (数组下标, 字段) 在原始文本中定位并仅替换该字段的
值字节，其余内容原样保留；写前强制校验"补丁后文本解析结果 == 内存修改后数据"，
不一致则拒绝写盘。
"""
from __future__ import annotations
import argparse
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 简体化安全网：语料整体已简体化，任何写回的文本新值都转简体，
# 避免把 gushiwen 的繁体标题/正文（含裁判接受的冲突覆盖）写进简体语料。
try:
    import zhconv
    def _simp(v):
        return zhconv.convert(v, "zh-cn")
except Exception:
    def _simp(v):
        return v


def _simplify_new(new):
    if isinstance(new, list):
        return [_simp(str(x)) for x in new]
    if isinstance(new, str):
        return _simp(new)
    return new


# ----------------------- 文本级 JSON 补丁 -----------------------
def find_top_level_spans(raw: str):
    """返回顶层数组每个【对象】元素的字符区间 [(start, end)]（end 为 '}' 之后一字符）。

    元素为非对象（标量）时返回 None，调用方应放弃补丁（保守）。
    """
    try:
        i = raw.index("[")
    except ValueError:
        return None
    spans = []
    n = len(raw)
    depth = 0
    in_str = False
    esc = False
    elem_start = None
    while i < n:
        c = raw[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c in "[{":
                depth += 1
                if depth == 2 and elem_start is None:
                    if c != "{":
                        return None  # 顶层元素不是对象 → 放弃
                    elem_start = i
            elif c in "]}":
                if depth == 2 and elem_start is not None:
                    if c != "}":
                        return None
                    spans.append((elem_start, i + 1))
                    elem_start = None
                depth -= 1
                if depth == 0:
                    break
        i += 1
    return spans


def _scan_value_span(seg: str, pos: int):
    """seg[pos] 为值首字符，返回 (start, end)。支持字符串/数组/对象/标量。"""
    c = seg[pos]
    if c == '"':
        i = pos + 1
        esc = False
        while i < len(seg):
            ch = seg[i]
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                return pos, i + 1
            i += 1
        return None
    if c in "[{":
        close = "]" if c == "[" else "}"
        depth = 0
        in_str = False
        esc = False
        i = pos
        while i < len(seg):
            ch = seg[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch in "[{":
                    depth += 1
                elif ch in "]}":
                    depth -= 1
                    if depth == 0:
                        return pos, i + 1
            i += 1
        return None
    m = re.match(r"[^,\}\]]+", seg[pos:])
    if not m:
        return None
    return pos, pos + len(m.group(0).rstrip())


def _find_key_value_span(seg: str, field: str):
    """在元素文本 seg 内找直属键 field 的值区间 (vstart, vend)；找不到返回 None。"""
    key = f'"{field}"'
    i = 0
    n = len(seg)
    in_str = False
    esc = False
    while i < n:
        c = seg[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            # 判断是否为键（后面跟冒号，且位于对象直属层）
            if seg.startswith(key, i):
                j = i + len(key)
                k = j
                while k < n and seg[k] in " \t":
                    k += 1
                if k < n and seg[k] == ":":
                    # 确认直属：从元素起点统计括号深度应为 1
                    depth = 0
                    for ch in seg[:i]:
                        if ch in "[{":
                            depth += 1
                        elif ch in "]}":
                            depth -= 1
                    if depth == 1:
                        vpos = k + 1
                        while vpos < n and seg[vpos] in " \t":
                            vpos += 1
                        span = _scan_value_span(seg, vpos)
                        if span:
                            return span
            in_str = True
            i += 1
            continue
        i += 1
    return None


def _dump_value_like(val, orig_text: str) -> str:
    """按原值文本的格式风格序列化新值（行内/多行、缩进、逗号后是否带空格）。

    目标是让 git diff 只呈现真实内容变化：原值为多行数组则新值也多行且缩进一致；
    原值为行内则新值也行内。
    """
    if not isinstance(val, (list, dict)):
        return json.dumps(val, ensure_ascii=False)
    if "\n" not in orig_text:
        sep = (", ", ": ") if ", " in orig_text else (",", ": ")
        return json.dumps(val, ensure_ascii=False, separators=sep)
    # 多行风格：从原值推导条目缩进与收尾缩进
    m_item = re.search(r"\n(\s+)", orig_text)
    m_close = re.search(r"\n(\s+)[\]\}]\s*$", orig_text)
    item_ind = m_item.group(1) if m_item else "  "
    close_ind = m_close.group(1) if m_close else item_ind
    trailing = bool(re.search(r", \n", orig_text))
    item_sep = ", \n" if trailing else ",\n"
    if isinstance(val, list):
        if not val:
            return "[]"
        body = item_sep.join(item_ind + json.dumps(x, ensure_ascii=False) for x in val)
        open_ch, close_ch = "[", "]"
    else:
        if not val:
            return "{}"
        body = item_sep.join(
            item_ind + json.dumps(k, ensure_ascii=False) + ": " + json.dumps(v, ensure_ascii=False)
            for k, v in val.items()
        )
        open_ch, close_ch = "{", "}"
    return f"{open_ch}\n{body}\n{close_ind}{close_ch}"


def patch_element(raw: str, span, changes: dict) -> str:
    """对 raw[span[0]:span[1]] 这一元素应用 {field: new_value} 补丁，返回新 raw。"""
    s, e = span
    seg = raw[s:e]
    ops = []  # (vstart, vend, new_text) 或 ("__insert__", field, new_json)
    for field, val in changes.items():
        vs = _find_key_value_span(seg, field)
        if vs is None:
            ops.append(("__insert__", field, json.dumps(val, ensure_ascii=False)))
        else:
            ops.append((vs[0], vs[1], _dump_value_like(val, seg[vs[0]:vs[1]])))
    # 先做替换（按位置从右往左，避免偏移失效）
    for vstart, vend, new_json in sorted(
        [o for o in ops if o[0] != "__insert__"], key=lambda x: x[0], reverse=True
    ):
        seg = seg[:vstart] + new_json + seg[vend:]
    # 再做插入（字段原本不存在）：插在元素结束 '}' 之前
    for op in [o for o in ops if o[0] == "__insert__"]:
        _, field, new_json = op
        # 键缩进：取元素内首个键行的前导空白；行内对象则退化为元素缩进+2
        m = re.search(r"\{\s*\n(\s+)", seg)
        if m:
            key_indent = m.group(1)
        else:
            elem_indent = re.match(r"[ \t]*", raw[:s].rsplit("\n", 1)[-1]).group(0)
            key_indent = elem_indent + "  "
        # '}' 前最后一个非空白字符：是 ',' 则无需再补逗号
        j = len(seg) - 2  # 跳过结尾的 '}'
        while j > 0 and seg[j] in " \t\n\r":
            j -= 1
        has_comma = seg[j] == ","
        insert = f'{"" if has_comma else ","}\n{key_indent}"{field}": {new_json}'
        seg = seg[:j + 1] + insert + seg[j + 1:]
    return raw[:s] + seg + raw[e:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True, help="核对报告 report.json 路径")
    ap.add_argument("--root", default=ROOT, help="数据根目录")
    ap.add_argument("--dry-run", action="store_true", help="只预演不改盘")
    args = ap.parse_args()

    rep = json.load(open(args.report, encoding="utf-8"))
    applied = rep.get("applied", [])

    # 按文件分组
    files = {}
    for c in applied:
        files.setdefault(c["file"], []).append(c)

    total_applied = total_skipped = 0
    for rel, items in sorted(files.items()):
        path = os.path.join(args.root, rel)
        if not os.path.isfile(path):
            print(f"[跳过] 文件不存在: {rel}")
            continue
        raw = open(path, encoding="utf-8").read()
        try:
            data = json.loads(raw)
        except Exception as ex:
            print(f"[跳过] {rel}: 原文件不是合法 JSON ({ex})")
            continue
        spans = find_top_level_spans(raw)
        if spans is None or len(spans) != len(data):
            print(f"[跳过] {rel}: 顶层结构不支持文本补丁（元素数 {len(spans or [])} vs {len(data)}）")
            continue
        # uid 规则与 schemas.make_record 完全一致：obj 有 id 则 uid=id，否则 uid=file#idx
        idx_of = {}
        for i, o in enumerate(data):
            if isinstance(o, dict):
                idx_of[str(o.get("id") or f"{rel}#{i}")] = i

        # 汇总每个元素的下标 -> {field: new}
        per_elem = {}
        f_skipped = 0
        for c in items:
            uid = c["uid"]
            if uid not in idx_of:
                print(f"  [跳过] uid 未命中: {uid}")
                f_skipped += 1
                continue
            idx = idx_of[uid]
            obj = data[idx]
            f = c["field"]
            new = _simplify_new(c["new"])
            if obj.get(f) == new:
                f_skipped += 1
                continue
            per_elem.setdefault(idx, {})[f] = new

        patched = raw
        for idx in sorted(per_elem, reverse=True):
            patched = patch_element(patched, spans[idx], per_elem[idx])
        n_changes = sum(len(v) for v in per_elem.values())

        if not args.dry_run:
            # 强制校验：补丁后文本必须能解析出与内存修改后完全一致的数据
            try:
                after = json.loads(patched)
            except Exception as ex:
                print(f"[拒绝] {rel}: 补丁后不是合法 JSON ({ex})，未写盘")
                continue
            expect = json.loads(raw)
            for idx, chs in per_elem.items():
                for f, v in chs.items():
                    expect[idx][f] = v
            if after != expect:
                print(f"[拒绝] {rel}: 补丁后数据与预期不一致，未写盘")
                continue
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(patched)
        print(f"  {rel}: 应用 {n_changes} 字段修正（涉及 {len(per_elem)} 条记录） 跳过 {f_skipped}")
        total_applied += n_changes
        total_skipped += f_skipped

    print(f"\n合计: 应用 {total_applied} 字段修正, 跳过 {total_skipped}"
          + ("  [dry-run 未写盘]" if args.dry_run else ""))


if __name__ == "__main__":
    main()
