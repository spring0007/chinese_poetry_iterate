#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
把被写成「一整条内含 \\n 的字符串」的正文字段，还原成源库原生的**数组**形态。

## 起因

源库正文字段（paragraphs / content / para）原生是 **list，一句一行**：

    "paragraphs": [
      "古径约城斜，锄荒可过车。",
      "直穿深筿去，不比绕村赊。"
    ],

但 `patch_file()` 当年只支持写字符串值（`json.dumps(str)` 直接塞回去），
于是 2026-09-30 那次百科订正把 286 个文件写成了：

    "paragraphs": "古径约城斜，锄荒可过车。\n直穿深筿去，不比绕村赊。",

视觉排版全毁、下游按行展示的逻辑也会读到一整坨。

## 判定规则（实测过的，不会误伤）

对全库每个集合抽样 git HEAD 版本统计：`youmengying`（幽梦影）原生就是 str，
其余集合**全部原生 list，且 HEAD 里没有任何一个含 \\n 的字符串**。
所以：

    body 是 str 且含 "\\n"  ⇒  一定是我们写坏的，改成 list
    youmengying 集合        ⇒  整体跳过（它的 str 是原生形态）

## 排版从哪来

被覆盖时原数组的缩进/换行已经丢了。这里从**同文件里一条未被改动的同类记录**
那里把 `paragraphs: [ … ]` 的原文本抠出来当模板（`render_list_like` 会复用它的
元素前缀、分隔符、行尾空格），所以还原后的缩进与原文一致，而不是猜一个。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sources as SRC                                   # noqa: E402
import patchjson as CX                                  # noqa: E402

WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xcheck")

# 原生就不是数组形态的集合，别动它
STR_NATIVE = {"youmengying"}


def _read(path: str) -> tuple:
    raw = open(path, "rb").read()
    bom = raw[:3] == b"\xef\xbb\xbf"
    return bom, raw.decode("utf-8-sig")


def _plan_file(coll, path: str) -> tuple:
    """返回 (待修 dict{idx: [lines]}, 模板文本, 消息)。"""
    bom, t = _read(path)
    spans = CX.top_spans(t)
    if not spans:
        return {}, "", "扫不出顶层记录"
    data = json.loads(t)
    if isinstance(data, dict):
        data = [data]

    field = coll.body
    fix = {}
    template = ""
    n_list = 0
    # 先扫一遍：既找排版模板，也数一下这个文件里还有多少条保持着原生数组形态。
    # why 需要 n_list 这个保护：万一某个集合我判断错了（它原生就是字符串），
    # 只要文件里还有若干条原生的 list，就说明「本该是数组」这个结论成立；
    # 若一条都没有，宁可跳过并报警，也不能凭猜想去改语料。
    for k, (s, e) in enumerate(spans):
        v = data[k].get(field) if k < len(data) else None
        obj = t[s:e]
        hit = CX._find_key(obj, field)
        raw_text = obj[hit[2]:hit[3]] if hit else ""
        if isinstance(v, list):
            n_list += 1
        if not template and isinstance(v, list) and raw_text.lstrip().startswith("[") \
                and raw_text.count('"') > 2:
            template = raw_text
    if not template or n_list == 0:
        return {}, "", "" if n_list == 0 else "没有可用的数组排版模板"

    for k, (s, e) in enumerate(spans):
        v = data[k].get(field) if k < len(data) else None
        # 原生是 str 的集合已整体跳过；这里凡是 str 一律是被错误的 ISO 覆盖的产物。
        # 单句（无换行）也要还原 —— 源库对单句同样存 ["一句"]，不是 "一句"。
        if isinstance(v, str) and v.strip():
            fix[k] = [ln for ln in v.split("\n") if ln.strip()] or [v.strip()]
    return fix, template, ""


def _apply_file(coll, path: str, fix: dict, template: str, dry: bool) -> tuple:
    """按源于原文的排版把字符串还原成数组。返回 (修复条数, 消息)。"""
    bom, t = _read(path)
    spans = CX.top_spans(t)
    data = json.loads(t)
    if isinstance(data, dict):
        data = [data]

    chunks = [t[:spans[0][0]]]
    for k, (s, e) in enumerate(spans):
        obj = t[s:e]
        if k in fix:
            if template:
                lit = CX.render_list_like(template, fix[k])
            else:
                # 同文件居然找不到多行数组模板 ⇒ 退化为标准 2 空格缩进
                lit = json.dumps(fix[k], ensure_ascii=False, indent=2)
            obj = CX.set_field(obj, coll.body, lit)
        chunks.append(obj)
        nxt = spans[k + 1][0] if k + 1 < len(spans) else len(t)
        chunks.append(t[e:nxt])
    new_text = "".join(chunks)

    # ---- 落盘前校验
    nd = json.loads(new_text)
    if isinstance(nd, dict):
        nd = [nd]
    if len(nd) != len(data):
        return 0, "记录数变了（%d → %d）—— 拒绝写入" % (len(data), len(nd))
    for k, lines in fix.items():
        got = nd[k].get(coll.body)
        if not isinstance(got, list) or got != lines:
            return 0, "第 %d 条还原后取值不符：%r" % (k, got)
    for k in range(len(data)):
        if k in fix:
            continue
        if data[k] != nd[k]:
            return 0, "第 %d 条未被修改却发生了变化 —— 拒绝写入" % k

    if dry:
        return len(fix), "校验通过"

    bak_dir = os.path.join(WORK, "fixjson-backup", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(bak_dir, exist_ok=True)
    rel = os.path.basename(path)
    shutil.copy2(path, os.path.join(bak_dir, rel))
    tmp = path + ".tmp-fixjson"
    with open(tmp, "wb") as fh:
        fh.write((b"\xef\xbb\xbf" if bom else b"") + new_text.encode("utf-8"))
    os.replace(tmp, path)
    return len(fix), "已写回（备份 %s）" % os.path.join("xcheck/fixjson-backup",
                                                     os.path.basename(bak_dir), rel)


def main() -> int:
    ap = argparse.ArgumentParser(description="把写坏的正文字段还原成数组形态")
    ap.add_argument("--only", default="", help="只处理某个集合名")
    ap.add_argument("--apply", action="store_true", help="写盘；默认只演练")
    ap.add_argument("--show", type=int, default=1, help="打印几条还原样例")
    args = ap.parse_args()

    total = files_hit = failed = 0
    samples = []
    for coll in SRC.COLLECTIONS:
        if args.only and coll.name != args.only:
            continue
        if coll.name in STR_NATIVE or coll.external:
            continue
        import glob
        for path in sorted(glob.glob(os.path.join(SRC.ROOT, coll.pattern.replace("\\", "/")))):
            if not os.path.isfile(path):
                continue
            try:
                fix, template, err = _plan_file(coll, path)
            except Exception as e:
                print("  ! %s 读取失败：%s" % (os.path.basename(path), e))
                failed += 1
                continue
            if err:
                continue
            if not fix:
                continue
            files_hit += 1
            total += len(fix)
            if len(samples) < args.show:
                k = sorted(fix)[0]
                samples.append((coll.name, os.path.basename(path), k, fix[k]))
            if not args.apply:
                continue
            n, msg = _apply_file(coll, path, fix, template, dry=False)
            if n == 0:
                failed += 1
                print("  ! %s：%s" % (os.path.basename(path), msg))

    print("\n=== 汇总 ===")
    print("需要还原的记录 %d 条，分布在 %d 个文件" % (total, files_hit))
    for name, base, k, lines in samples:
        print("\n  样例 %s/%s 第 %d 条，还原为 %d 行：" % (name, base, k, len(lines)))
        for ln in lines[:4]:
            print("      %s" % ln)
    if failed:
        print("\n失败文件 %d 个" % failed)
    print("\n%s" % ("已写盘，备份在 xcheck/fixjson-backup/"
                    if args.apply else "[演练] 未写盘。加 --apply 落盘。"))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
