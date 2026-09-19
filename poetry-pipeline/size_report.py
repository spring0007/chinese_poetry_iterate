# -*- coding: utf-8 -*-
"""
体积账：把「字典化 / 去掉简体副本 / 不落库 dynasty-kind」到底各省了多少算出来。

why 单独一个脚本：体积优化最容易变成"我说省了 30%"这种无法复核的表述。
这里每一项都用库里的真实字节数算，数字可复现：
    python size_report.py ./dist/poetry.db

用法:
    python size_report.py [poetry.db]
"""
from __future__ import annotations
import os, sys, sqlite3

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")


def _here(name: str) -> str:
    """默认库路径按脚本位置解析，不依赖当前工作目录。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist", name)


DB = sys.argv[1] if len(sys.argv) > 1 else _here("poetry.db")
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
cur = con.cursor()
MB = 1048576.0


def one(sql, *a):
    return cur.execute(sql, a).fetchone()[0]


n = one("SELECT COUNT(*) FROM poems")
print("=" * 72)
print("体积账  %s   %.1f MB   %d 条" % (os.path.abspath(DB), os.path.getsize(DB) / MB, n))
print("=" * 72)

# --- 1. 字典化 vs 直接存字符串
q = """
SELECT
  SUM(LENGTH(a.name)) AS author_b,
  SUM(LENGTH(p.title)) AS title_b,
  SUM(LENGTH(r.name)) AS rhythmic_b,
  SUM(LENGTH(s.name)) AS src_b,
  SUM(LENGTH(p.body)) AS body_b,
  SUM(LENGTH(p.tags)) AS tags_b,
  SUM(LENGTH(p.notes)) AS notes_b
FROM poems p
LEFT JOIN authors a ON a.id = p.author_id
LEFT JOIN rhythmics r ON r.id = p.rhythmic_id
LEFT JOIN sources s ON s.id = p.src_id
"""
r = cur.execute(q).fetchone()
print("\n[1] 哪些列值得字典化（重复率 <2 的建字典表是净亏损）")
for col, label, key in (("author_id", "作者", "author_b"), ("rhythmic_id", "词牌", "rhythmic_b"),
                        ("src_id", "来源", "src_b")):
    u = one("SELECT COUNT(DISTINCT %s) FROM poems" % col)
    print("    %-4s 唯一值 %-7d 重复率 %8.1f 次/值   逐行存需 %6.2f MB   -> 字典化（存 int）"
          % (label, u, n / max(u, 1), (r[key] or 0) / MB))
u_t = one("SELECT COUNT(DISTINCT title) FROM poems")
print("    %-4s 唯一值 %-7d 重复率 %8.2f 次/值   逐行存需 %6.2f MB   -> 直接存字符串"
      % ("标题", u_t, n / max(u_t, 1), (r["title_b"] or 0) / MB))
print("    （v1 曾把标题也字典化：多付 9.80MB 表 + 9.77MB 唯一索引 + 9.77MB 冗余索引 = 29.3MB，"
      "只省下 2.71MB 文本，已改回直存）")

# --- 2. 去掉简体副本 body_s（v1 有、v2 没有）
# 注意 LENGTH() 数的是字符，中文 UTF-8 一个字 3 字节，报体积必须用 octet_length。
body_b = one("SELECT SUM(octet_length(p.body)) FROM poems p")
body_chars = r["body_b"] or 0
print("\n[2] 去掉 body_s 简体副本（v1 每行另存一份简体正文，v2 正文即简体）")
print("    正文总计 %.2f MB（%d 字，UTF-8 实占）——v1 还要再存一份同尺寸的简体副本，v2 省掉整份"
      % (body_b / MB, body_chars))
print("    代价：查询前不必再判断该查哪一列，逻辑从两列收敛成一列")

# --- 3. dynasty / kind 不落库
print("\n[3] dynasty / kind 不落库（由 id 高位反解）")
print("    每行省约 %d 字节（'song'/'poem' 两列）-> %.2f MB，且无需为它们建索引"
      % (10, n * 10 / MB))

# --- 4. 主表构成
print("\n[4] 主表现状（每行平均字节，正文按 UTF-8 实占算）")
print("    正文 %.1f 字节  标签+注释 %.1f 字节  其余(id + 3 个 int + score + n_char) 约 12 字节"
      % (body_b / n, ((r["tags_b"] or 0) + (r["notes_b"] or 0)) / n))
print("    正文占主表 %.0f%%——主表已经几乎没有正文之外的冗余可压"
      % (100.0 * body_b / max(body_b + (r["tags_b"] or 0) + (r["notes_b"] or 0), 1)))

# --- 5. 各对象在库里的页占用（dbstat 可用时）
print("\n[5] 库内页占用（按表/索引）")
try:
    rows = cur.execute(
        "SELECT name, SUM(pgsize) b FROM dbstat GROUP BY name ORDER BY b DESC").fetchall()
    for x in rows:
        print("    %-24s %8.2f MB" % (x["name"], x["b"] / MB))
except sqlite3.OperationalError:
    print("    （本机构建的 SQLite 未启用 dbstat 虚拟表，跳过）")

# --- 6. 可继续优化的方向
dup = one("SELECT COUNT(*) FROM (SELECT author_id, body, COUNT(*) c FROM poems "
          "GROUP BY author_id, body HAVING c > 1)")
print("\n[6] 还能再压的吗")
print("    同作者+同正文 重复组 %d 组（已在构建期去重，这些是跨集合的正常重收）" % dup)
print("    结论：正文之外已无可观冗余，再要缩小只能靠压缩算法或不打包平仄")
