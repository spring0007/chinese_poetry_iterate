# -*- coding: utf-8 -*-
"""多首合页修复的离线验证：用真实缓存页测试片段截取与夹注剥离。

用例（全部来自 verify_output_small 实测数据）：
1. 皇甫松《摘得新》二首合页：#22 本地为第二首（埋在页中）、#23 本地为第一首（页首）。
2. 皇甫松《天仙子》二首合页（第二首行首带夹注）。
3. 杜牧《山行》单页：本地讹字"远目" + 页面夹注"(生处 一作：深处)"。
4. 刘长卿《送灵澈》单页：页面有独立夹注行"(斜阳 一作：夕阳)"。
5. 卢纶《晚次鄂州》：本地只有前半首（截断），不得把后半首并进来。
6. 潘阆《酒泉子·长忆孤山》：正常单页消歧仍应命中。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from poetry_verifier.retrievers.base import Cache, HttpClient
from poetry_verifier.retrievers.gushiwen import GushiwenRetriever


class _Req:
    retries = 2
    backoff_base = 1
    backoff_max = 10
    timeout = 15


CACHE = "verify_output_small/.cache"


def load_local(path, idx):
    return json.load(open(path, encoding="utf-8"))[idx]


def main():
    cache = Cache(CACHE)
    http = HttpClient(_Req(), cache, 0.2)
    gw = GushiwenRetriever(http, 4, 0.82)

    def ref_of(author, title, rhythmic, local_lines):
        return gw.resolve(title, author, rhythmic, local_lines)

    fails = []

    def check(name, cond, detail=""):
        print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" | {detail}" if detail else ""))
        if not cond:
            fails.append(name)

    # ---- 1. 皇甫松《摘得新》二首合页 ----
    hj = "五代诗词/huajianji/huajianji-2-juan.json"
    rec22 = load_local(hj, 22)
    rec23 = load_local(hj, 23)
    r22 = ref_of("皇甫松", rec22.get("title"), rec23.get("rhythmic") or rec22.get("rhythmic"), rec22["paragraphs"])
    print(f"\n[摘得新#22 本地第二首] 首句: {rec22['paragraphs'][0][:12]}")
    if r22 is None:
        check("摘得新#22 有参考", False, "resolve 返回 None")
    else:
        joined = "".join(r22.content_lines)
        check("摘得新#22 参考=本地那一首", "酌一卮" in joined and "摘得新，枝枝叶叶春" not in joined,
              f"参考 {len(r22.content_lines)} 行: {joined[:30]}...")
    r23 = ref_of("皇甫松", rec23.get("title"), rec23.get("rhythmic") or rec23.get("rhythmic"), rec23["paragraphs"])
    print(f"[摘得新#23 本地第一首] 首句: {rec23['paragraphs'][0][:12]}")
    if r23 is None:
        check("摘得新#23 有参考", False, "resolve 返回 None")
    else:
        joined = "".join(r23.content_lines)
        check("摘得新#23 参考未并入第二首", "摘得新" in joined and "酌一卮" not in joined,
              f"参考 {len(r23.content_lines)} 行: {joined[:30]}...")

    # ---- 2. 皇甫松《天仙子》合页 ----
    rec17 = load_local(hj, 17)
    r17 = ref_of("皇甫松", rec17.get("title"), rec17.get("rhythmic"), rec17["paragraphs"])
    print(f"\n[天仙子#17] 本地首句: {rec17['paragraphs'][0][:12]}")
    if r17 is None:
        check("天仙子#17 有参考", False, "resolve 返回 None")
    else:
        joined = "".join(r17.content_lines)
        check("天仙子#17 参考=本地那一首（无夹注、无另一首）",
              "踯躅花开" in joined and "晴野鹭鸶" not in joined and "一作" not in joined,
              f"参考 {len(r17.content_lines)} 行: {joined[:36]}...")

    # ---- 3. 杜牧《山行》夹注 ----
    sm = "水墨唐诗/shuimotangshi.json"
    rec138 = load_local(sm, 138)
    r138 = ref_of("杜牧", rec138.get("title"), None, rec138["paragraphs"])
    print(f"\n[山行#138] 本地: {' / '.join(rec138['paragraphs'])[:40]}")
    if r138 is None:
        check("山行#138 有参考", False, "resolve 返回 None")
    else:
        joined = "".join(r138.content_lines)
        check("山行#138 夹注已剥离", "一作" not in joined, f"参考: {joined[:40]}")
        check("山行#138 权威为正确用字", "远上寒山" in joined, f"参考: {joined[:20]}")

    # ---- 4. 刘长卿《送灵澈》独立夹注行 ----
    rec62 = load_local(sm, 62)
    r62 = ref_of("刘长卿", rec62.get("title"), None, rec62["paragraphs"])
    print(f"\n[送灵澈#62] 本地: {' / '.join(rec62['paragraphs'])[:40]}")
    if r62 is None:
        check("送灵澈#62 有参考", False, "resolve 返回 None")
    else:
        joined = "".join(r62.content_lines)
        check("送灵澈#62 独立夹注行已剔除", "一作" not in joined and "斜阳" not in joined.replace("荷笠带斜阳", ""),
              f"参考 {len(r62.content_lines)} 行: {joined[:36]}")

    # ---- 5. 卢纶《晚次鄂州》截断不得并入 ----
    rec12 = load_local(sm, 12)
    r12 = ref_of("卢纶", rec12.get("title"), None, rec12["paragraphs"])
    print(f"\n[晚次鄂州#12] 本地 {len(rec12['paragraphs'])} 行（疑截断）")
    if r12 is None:
        check("晚次鄂州#12 有参考", False, "resolve 返回 None")
    else:
        joined = "".join(r12.content_lines)
        check("晚次鄂州#12 未把后半首并入参考", "三湘衰鬓" not in joined,
              f"参考 {len(r12.content_lines)} 行（应与本地等长）: {joined[:30]}")

    # ---- 6. 潘阆《酒泉子·长忆孤山》正常消歧 ----
    cs = "宋词/ci.song.0.json"
    pan = None
    for o in json.load(open(cs, encoding="utf-8")):
        if o.get("author") == "潘阆" and o.get("rhythmic") == "酒泉子" and o["paragraphs"][0].startswith("长忆孤山"):
            pan = o
            break
    print(f"\n[潘阆·酒泉子·长忆孤山] 本地首句: {pan['paragraphs'][0][:12]}")
    rp = ref_of("潘阆", None, "酒泉子", pan["paragraphs"])
    check("潘阆长忆孤山仍能精确命中", rp is not None and "孤山" in (rp.title or "") + "".join(rp.content_lines),
          f"标题={rp.title if rp else None}")

    print("\n" + ("全部通过 ✓" if not fails else f"失败 {len(fails)} 项: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
