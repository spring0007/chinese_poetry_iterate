# -*- coding: utf-8 -*-
"""离线验证 _looks_like_different_poem 与 decide() 的错配拦截。"""
import sys, os, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from poetry_verifier.verifier import decide, _looks_like_different_poem
from poetry_verifier.schemas import PoemRecord
from poetry_verifier.retrievers.base import Reference

def mkrec(title, author, content_lines, rhythmic=None, dynasty=None):
    obj = {"title": title, "author": author}
    if dynasty: obj["dynasty"] = dynasty
    return PoemRecord(file_rel="X", idx=0, obj=obj, title=title, author=author,
                      rhythmic=rhythmic, content_key="paragraphs",
                      content_lines=content_lines, uid="u1")

def mkref(title, author, content_lines, dynasty=None):
    return Reference(source_name="t", url="u", title=title, author=author,
                     dynasty=dynasty, content_lines=content_lines, raw_content="")

def settings():
    v = types.SimpleNamespace(fill_placeholders=True, confidence_threshold=0.8,
                              add_missing_fields=False, overwrite_conflicts=False)
    return types.SimpleNamespace(verify=v)

# 用例
local_ly = ["长安古道马迟迟。", "高柳乱蝉嘶。", "夕阳岛外，秋风原上，目断四天垂。",
            "归云一去无踪迹，何处是前期。"]
ref_ly_same = ["长安古道马迟迟，", "高柳乱蝉嘶。", "夕阳岛外，秋风原上，目断四天垂。",
               "归云一去无踪迹，何处是前期。"]  # 同首，仅断句/标点差异
ref_ly_wrong = ["参差烟树灞陵桥。", "风物尽前朝。", "衰杨古柳，几经攀折，憔悴楚宫腰。",
                "夕阳闲淡秋光老，离思满蘅皋。"]  # 柳永另一首《少年游》

cases = [
    ("同首·异断句(应不拦截→冲突待复核)", local_ly, ref_ly_same, False),
    ("不同首·同词牌(应拦截→reference_mismatch)", local_ly, ref_ly_wrong, True),
]
print("=== _looks_like_different_poem 直接测试 ===")
for name, lo, rf, expect in cases:
    got = _looks_like_different_poem(lo, rf)
    print(f"  [{'OK' if got==expect else 'FAIL'}] {name}: flag={got} (期望 {expect})")

print("\n=== decide() 端到端 ===")
for name, lo, rf, expect_mismatch in cases:
    rec = mkrec("少年游", "柳永", lo)
    ref = mkref("少年游", "柳永", rf, "宋")
    d = decide(rec, ref, settings())
    is_mm = d["status"] == "reference_mismatch"
    # 期望：wrong→mismatch(True)；same→不是 mismatch
    ok = (is_mm == expect_mismatch)
    print(f"  [{'OK' if ok else 'FAIL'}] {name}: status={d['status']} (期望mismatch={expect_mismatch})")
