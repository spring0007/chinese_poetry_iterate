# -*- coding: utf-8 -*-
"""patchjson.py —— 语料 JSON 的「字节级最小改写」共用件

从 reconcile_gui.py 里抽出来，供两个界面共用：
  * reconcile_gui.py —— 差异校对界面，把「作者 / 标题」的裁定写回源 JSON；
  * app.py           —— 诗词编辑界面，把编辑过的字段写回源 JSON。

「改语料」这件事只允许有一份实现：它是全工程唯一会动原文件的地方，
必须只有一套校验、一套备份、一套原子落盘，否则两个入口迟早会走出两种行为。
（crosscheck.py 里另有一份同源的副本，那是打包前的历史遗留，本模块不依赖它、它也不依赖本模块。）

本模块只用标准库（os / re / json / shutil / time），可被 `--windowed` 打包的 exe 安全导入：
没有 `sys.stdout` 之类的模块级副作用，也不会自己去猜工程根目录。

核心是 patch_file()：只替换指定记录指定字段的 JSON 字面量，
其余字节（含转义、缩进、BOM、无关字段）一概原样搬运，写盘前复核，
复核不过就拒绝写盘——宁可不改，也不能改坏语料。
"""

from __future__ import annotations
import os
import re
import json
import shutil
import time

__all__ = ["patch_file", "set_field", "top_spans",
           "render_list_like", "literal_for", "text_of"]


# ============================================================ 字节级最小改写（复用 crosscheck 思路）
def _skip_str(t: str, i: int) -> int:
    i += 1
    while i < len(t):
        c = t[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        i += 1
    return len(t)


def _skip_val(t: str, i: int) -> int:
    c = t[i]
    if c == '"':
        return _skip_str(t, i)
    if c in "{[":
        depth, n, instr = 0, len(t), False
        while i < n:
            ch = t[i]
            if instr:
                if ch == "\\":
                    i += 2
                    continue
                if ch == '"':
                    instr = False
            elif ch == '"':
                instr = True
            elif ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return n
    m = re.compile(r"[-+0-9.eEtrufalsn]+").match(t, i)
    return m.end() if m else i + 1


def top_spans(t: str) -> list:
    i = 0
    while i < len(t) and t[i] in " \t\r\n﻿":
        i += 1
    if i >= len(t):
        return []
    if t[i] == "{":
        return [(i, _skip_val(t, i))]
    if t[i] != "[":
        return []
    out, j = [], i + 1
    while True:
        while j < len(t) and t[j] in " \t\r\n,":
            j += 1
        if j >= len(t) or t[j] == "]":
            break
        e = _skip_val(t, j)
        out.append((j, e))
        j = e
    return out


def _find_key(t: str, key: str):
    for m in re.finditer(r'"%s"' % re.escape(key), t):
        s = m.end()
        k = s
        while k < len(t) and t[k] in " \t\r\n":
            k += 1
        if k >= len(t) or t[k] != ":":
            continue
        k += 1
        while k < len(t) and t[k] in " \t\r\n":
            k += 1
        return m.start(), m.end(), k, _skip_val(t, k)
    return None


def set_field(obj_text: str, key: str, new_literal: str) -> str:
    hit = _find_key(obj_text, key)
    if hit:
        a, _b, vs, ve = hit
        return obj_text[:vs] + new_literal + obj_text[ve:]
    i = obj_text.find("{")
    if i < 0:
        raise ValueError("不是对象")
    j = i + 1
    indent = ""
    while j < len(obj_text) and obj_text[j] in " \t\r\n":
        indent += obj_text[j]
        j += 1
    ins = '"%s": %s,' % (key, new_literal)
    if indent.startswith("\n"):
        return obj_text[:i + 1] + "\n" + indent.lstrip("\r\n") + ins.lstrip() + obj_text[j:]
    return obj_text[:i + 1] + ins + obj_text[j:]


def text_of(v) -> str:
    """把字段值拉平成文本：list 按行 join，str 原样。用于跨类型比较。"""
    if isinstance(v, list):
        return "\n".join(str(x) for x in v)
    return v if isinstance(v, str) else ""


def render_list_like(old_text: str, items: list) -> str:
    """
    按原字段的排版把一组字符串渲染成 JSON 数组字面量。

    why 不能用 `json.dumps(items)`：那会得到紧凑单行 `["a","b"]`。源库正文是
    **每行一句、缩进对齐**的排版（不少分片行尾还带一个空格），直接 dumps 会把
    整个文件的视觉风格打乱。这里把「元素前缀 / 分隔符 / 元素后缀」三段空白
    原样从旧文本里抠出来复用，所以还原后的缩进与原文一模一样。
    """
    i, j = old_text.find("["), old_text.rfind("]")
    if i < 0 or j < 0 or not old_text[i + 1:j].strip():
        return json.dumps(items, ensure_ascii=False)
    inner = old_text[i + 1:j]

    k = inner.find('"')
    head = inner[:k] if k >= 0 else " "
    m = inner.rfind('"')
    tail = inner[m + 1:] if m >= 0 else " "

    sep = ", "
    if k >= 0:
        mm = re.match(r'"(?:[^"\\]|\\.)*"', inner[k:])
        if mm:
            nxt = inner.find('"', k + mm.end())
            if nxt >= 0:
                sep = inner[k + mm.end():nxt]
    if not items:
        return "[]"
    return "[" + head + sep.join(json.dumps(x, ensure_ascii=False) for x in items) + tail + "]"


def literal_for(seg: str, idx: int, field: str, val, data: list) -> str:
    """
    给「第 idx 条记录的 field」生成写回用的 JSON 字面量，**保持原字段类型**。

    why 关键：源库正文字段多数是**数组**（一句一行，见 `"paragraphs": [ … ]`），
    但参照站取回来的、界面里编辑出来的都是 join 好的字符串。以前这里无脑
    `json.dumps(字符串)` 塞回去，就把数组覆盖成了一整个内含 `\\n` 的字符串——
    2026-09-30 那次百科订正把 286 个文件写成了这样，视觉排版全毁。
    现在先看原值类型：原来是数组就按 \\n 拆回数组并按原排版渲染；
    原来就是字符串才原样写入。
    """
    old = data[idx].get(field) if idx < len(data) else None
    if not isinstance(old, list):
        return json.dumps(val, ensure_ascii=False)

    items = [ln for ln in str(val).split("\n") if ln.strip()]
    hit = _find_key(seg, field)
    if hit:
        old_text = seg[hit[2]:hit[3]]
        if old_text.lstrip().startswith("["):
            return render_list_like(old_text, items)
    return json.dumps(items, ensure_ascii=False, indent=2)


def _descend(doc, array_path):
    """按 array_path 从整篇文档下钻到「记录数组」。空路径＝顶层就是数组。
    下钻不到、或落到非列表，返回 None（调用方据此拒绝写入）。"""
    for key in array_path:
        if not isinstance(doc, dict) or key not in doc:
            return None
        doc = doc[key]
    if isinstance(doc, dict):
        doc = [doc]
    return doc if isinstance(doc, list) else None


def _spans_of(t: str, array_path):
    """记录数组各元素在整篇文本里的 [起, 止) 区间。定位不到返回 None。"""
    if not array_path:
        return top_spans(t)
    off = 0
    for key in array_path:
        hit = _find_key(t[off:], key)
        if not hit:
            return None
        off += hit[2]
    end = _skip_val(t, off)
    return [(off + s, off + e) for (s, e) in top_spans(t[off:end])]


def patch_file(path: str, edits: list, backup_dir: str, dry: bool = False,
               array_path: tuple = ()) -> dict:
    """edits: [(记录序号, 字段名, 新值)] 或 [(记录序号, 字段名, 期望旧值, 新值)]。只改这几处，其余原样。
    给四元组时多一道写前断言：该字段当前取值必须等于「期望旧值」，否则拒绝写入。
    调用方（app.py）用它把「我读到的旧值」和「文件里现在的值」钉在一起，
    挡住「读完到写之间文件被别人改了」这种时间差。

    array_path: 记录数组藏在哪个键下。默认 () ＝ 文件顶层就是记录数组（全唐诗/宋词等分片）；
                外接集合（external/*.json 的 spec 结构）传 ("items",)。

    返回 {"ok","msg","before","after"}。写盘前三条硬不变量（任一不成立即拒绝）：
      1) 给了期望旧值时，该字段当前取值必须相符；
      2) 被编辑的那条记录，写后取该字段的值必须等于要写的值；
      3) 未被编辑的记录逐条必须与改前完全相同，且记录数不变。
    """
    info = {"ok": False, "msg": "", "n": len(edits), "before": {}, "after": {}}
    edits = [(e[0], e[1], e[2], e[3]) if len(e) == 4
             else (e[0], e[1], None, e[2]) for e in edits]
    raw = open(path, "rb").read()
    bom = raw[:3] == b"\xef\xbb\xbf"
    t = raw.decode("utf-8-sig")
    spans = _spans_of(t, array_path)
    if spans is None:
        info["msg"] = "找不到记录数组所在的键 %s" % (array_path,)
        return info
    if not spans:
        info["msg"] = "扫不出顶层记录"
        return info
    data = _descend(json.loads(t), array_path)
    if data is None:
        info["msg"] = "记录数组不是列表"
        return info

    ordered = sorted(edits, key=lambda e: e[0])
    if any(not (0 <= i < len(spans)) for i, _f, _o, _v in ordered):
        info["msg"] = "记录序号越界（文件里只有 %d 条）" % len(spans)
        return info
    for idx, field, want_old, _v in ordered:
        # 同样按文本内容比对：界面里读到的正文往往是 join 后的字符串，
        # 而库里的原生形态是数组，直接比对象会恒等失败。
        if want_old is not None and text_of(data[idx].get(field)) != text_of(want_old):
            info["msg"] = "第 %d 条 %s 的当前取值与预期不符（期望 %r，实得 %r）——文件已被改动，拒绝写入" % (
                idx, field, want_old, data[idx].get(field))
            return info
    # 一条记录一次改多个字段是常态（标题+正文一起改），必须按序号归组后逐个落进去：
    # 早先写成「一个序号只取第一条」，第二条会被静默丢掉，再被下面的写后断言拦下——
    # 结果是整次写回白做（虽然不会改坏文件，但用户会看到「写入后取值不符」）。
    by_idx = {}
    for idx, field, _old, val in ordered:
        by_idx.setdefault(idx, []).append((field, val))

    a = spans[0][0]
    chunks = [t[:a]]
    for k, (s, e) in enumerate(spans):
        seg = t[s:e]
        for field, val in by_idx.get(k, ()):
            seg = set_field(seg, field, literal_for(seg, k, field, val, data))
        chunks.append(seg)
        nxt = spans[k + 1][0] if k + 1 < len(spans) else len(t)
        chunks.append(t[e:nxt])
    new_text = "".join(chunks)

    nd = _descend(json.loads(new_text), array_path)
    if nd is None:
        info["msg"] = "改后记录数组不是列表"
        return info
    changed = set(i for i, _f, _o, _v in edits)
    for _k, (idx, field, _old, val) in enumerate(edits):
        info["before"][idx] = data[idx].get(field)
        info["after"][idx] = nd[idx].get(field)
        # why 比 text_of 拉平后的文本而不是直接比对象：源库正文字段是 list，
        # 参照站/界面取回来的却是 join 过的字符串。写成 list 才保得住原排版，
        # 所以按「文本内容一致」判定，而不是「类型一致」。
        if text_of(nd[idx].get(field)) != text_of(val):
            info["msg"] = "第 %d 条 %s 写入后取值不符（期望 %r，实得 %r）" % (
                idx, field, val, nd[idx].get(field))
            return info
    for i in range(len(data)):
        if i in changed:
            continue
        if data[i] != nd[i]:
            info["msg"] = "第 %d 条未被修改却变了 —— 拒绝写入" % i
            return info
    if len(nd) != len(data):
        info["msg"] = "记录数变了 —— 拒绝写入"
        return info
    if dry:
        info["ok"] = True
        info["msg"] = "校验通过（演练，未写盘）"
        return info

    os.makedirs(backup_dir, exist_ok=True)
    bak = os.path.join(backup_dir, time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(bak, exist_ok=True)
    shutil.copy2(path, os.path.join(bak, os.path.basename(path)))
    tmp = path + ".tmp-patch"
    with open(tmp, "wb") as fh:
        fh.write((b"\xef\xbb\xbf" if bom else b"") + new_text.encode("utf-8"))
    os.replace(tmp, path)
    info["ok"] = True
    info["msg"] = "已写回（备份 %s）" % os.path.join(bak, os.path.basename(path))
    return info
