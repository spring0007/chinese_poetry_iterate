# -*- coding: utf-8 -*-
"""对构建产物做断言式校验：简体无空格 / 佚名置空 / 字典化去重 / 数据质量 / 平仄独立包。

用法:
    python verify_db.py ./dist/poetry.db
    python verify_db.py ./dist/poetry.db ./dist/poetry-strains.db   # 顺带校验平仄包
"""
from __future__ import annotations
import os, sys, sqlite3, collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema as S

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")


def _here(name: str) -> str:
    """默认库路径按脚本位置解析，不依赖当前工作目录（PyCharm 里右键运行也找得到）。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist", name)


DB = sys.argv[1] if len(sys.argv) > 1 else _here("poetry.db")
SDB = sys.argv[2] if len(sys.argv) > 2 else ""
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
cur = con.cursor()

ok = True


def chk(name, cond, detail=""):
    global ok
    flag = "PASS" if cond else "FAIL"
    if not cond:
        ok = False
    print("[%s] %-38s %s" % (flag, name, detail))


def one(sql, *a):
    return cur.execute(sql, a).fetchone()[0]


print("=" * 78)
print("数据库:", os.path.abspath(DB), " %.1f MB" % (os.path.getsize(DB) / 1048576))
print("=" * 78)

# ---------- 0. 版本戳
# 版本戳必须对得上，否则"这次校验的是哪一版产物"就无从判断——尤其是平仄已拆包，
# 主库版本 2.0 意味着平仄还在库里，后面的断言结论会完全不同。
_v_main = one("SELECT v FROM meta WHERE k='schema_version'")
chk("主库 schema_version = %s" % S.SCHEMA_VERSION, _v_main == S.SCHEMA_VERSION,
    "实际 %s" % _v_main)
chk("主库 text_form = simplified-nospace",
    one("SELECT v FROM meta WHERE k='text_form'") == "simplified-nospace")

# ---------- 1. 规模
n_poem = one("SELECT COUNT(*) FROM poems")
print("\n-- 规模 --")
print("  poems %d / authors %d / rhythmics %d / sources %d" % (
    n_poem, one("SELECT COUNT(*) FROM authors"), one("SELECT COUNT(*) FROM rhythmics"),
    one("SELECT COUNT(*) FROM sources")))

# 平仄必须是独立包：主库里出现 poem_strains 说明拆分没生效
has_tbl = one("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
              "AND name='poem_strains'")
chk("主库不含 poem_strains（平仄已独立）", has_tbl == 0,
    "主库仍有该表" if has_tbl else "")
if SDB:
    con.execute("ATTACH DATABASE ? AS strainsdb", (SDB,))
    n_st = one("SELECT COUNT(*) FROM strainsdb.poem_strains")
    n_join = one("SELECT COUNT(*) FROM strainsdb.poem_strains ps "
                 "JOIN poems p ON p.id=ps.id")
    chk("平仄包按 id 全部可关联主库", n_st == n_join,
        "平仄 %d 条 / 可关联 %d 条" % (n_st, n_join))
    print("  平仄包 %d 条（占主库 %.1f%%）" % (n_st, 100.0 * n_st / max(n_poem, 1)))

# ---------- 2. 简体：正文/标题/作者 转简体后应无变化
print("\n-- 要求1：全简体 --")
SP = tuple(S._SPACES)
bad_t = []

# 抽样 8000 条做繁体检测（全量 zhconv 太慢）
sample = cur.execute(
    "SELECT p.body, p.title, a.name FROM poems p "
    "LEFT JOIN authors a ON a.id=p.author_id "
    "LIMIT 8000").fetchall()
n_trad = 0
trad_chars = collections.Counter()
trad_samples = []
for r in sample:
    for v in (r[0], r[1] or "", r[2] or ""):
        if not v:
            continue
        c = S._zh_convert(v, "zh-cn")
        if c != v:
            n_trad += 1
            if len(trad_samples) < 8:
                trad_samples.append((v[:40], c[:40]))
            for i, ch in enumerate(v):
                if i < len(c) and c[i] != ch:
                    trad_chars[ch] += 1
            break
chk("抽样 8000 条无残留繁体", n_trad == 0, "残留 %d 条" % n_trad)
if n_trad:
    print("  差异字符 TOP20: %s" % ", ".join(
        "%s(%d)" % (k, v) for k, v in trad_chars.most_common(20)))
    for a, b in trad_samples:
        print("    原文 %s" % a.replace("\n", " / "))
        print("    转换 %s" % b.replace("\n", " / "))

# ---------- 3. 无空格
print("\n-- 要求1：无空格 --")
n_body_sp = 0
SPACE_CHARS = (" ", "\u3000", "\t", "\xa0")
cond = " OR ".join(["p.body LIKE '%%%s%%'" % c for c in SPACE_CHARS])
n_body_sp = one("SELECT COUNT(*) FROM poems p WHERE " + cond)
chk("正文无空格字符", n_body_sp == 0, "含空格 %d 行" % n_body_sp)

cond_t = " OR ".join(["p.title LIKE '%%%s%%'" % c for c in SPACE_CHARS])
n_t_sp = one("SELECT COUNT(*) FROM poems p WHERE " + cond_t)
chk("标题无空格", n_t_sp == 0, "含空格 %d 条" % n_t_sp)
n_a_sp = one("SELECT COUNT(*) FROM authors WHERE " + " OR ".join(
    ["name LIKE '%%%s%%'" % c for c in SPACE_CHARS]))
chk("作者名无空格", n_a_sp == 0, "含空格 %d 条" % n_a_sp)

# ---------- 4. 佚名置空
print("\n-- 要求2：佚名写为 NULL --")
ANON_TOKENS = ("佚名", "无名", "不详", "無名", "不詳", "匿名", "失名")
cond_a = " OR ".join(["name LIKE '%%%s%%'" % t for t in ANON_TOKENS])
n_anon_name = one("SELECT COUNT(*) FROM authors WHERE " + cond_a)
chk("authors 表无佚名条目", n_anon_name == 0, "残留 %d 条" % n_anon_name)

n_null = one("SELECT COUNT(*) FROM poems WHERE author_id=0")
chk("poems 中 author_id=0（即佚名）已置空", n_null > 0,
    "佚名 %d 条 (%.1f%%)" % (n_null, 100.0 * n_null / max(n_poem, 1)))

# 反查：author_id=0 的行，JOIN 后 author 必须是 None
r = cur.execute("SELECT COUNT(*) FROM poems p LEFT JOIN authors a ON a.id=p.author_id "
                "WHERE p.author_id=0 AND a.name IS NOT NULL").fetchone()[0]
chk("author_id=0 行 JOIN 后为 NULL", r == 0, "异常 %d 行" % r)

# 作者名不含"氏"的误伤检查（王氏/花蕊夫人徐氏 是真实姓名）
n_shi = one("SELECT COUNT(*) FROM authors WHERE name LIKE '%氏'")
print("  [INFO] 保留的 X氏 姓名（王氏/花蕊夫人徐氏等）: %d" % n_shi)

print("\n-- 要求3：减少重复数据 --")
n_title_uniq = one("SELECT COUNT(DISTINCT title) FROM poems")
print("  标题: %d 条记录 / %d 个唯一值，重复率 %.2f 次/值（<2 时不值得建字典表）"
      % (n_poem, n_title_uniq, n_poem / max(n_title_uniq, 1)))
for col, label in (("author_id", "作者"), ("rhythmic_id", "词牌"), ("src_id", "来源")):
    u = one("SELECT COUNT(DISTINCT %s) FROM poems" % col)
    print("  %-4s: %d 条记录 / %d 个唯一值，重复率 %.1f 次/值 -> 已字典化"
          % (label, n_poem, u, n_poem / max(u, 1)))

# 正文重复度（同一作者+正文出现多次 = 可进一步去重）
n_dup_body = one("SELECT COUNT(*) FROM (SELECT author_id, body, COUNT(*) c FROM poems "
                 "GROUP BY author_id, body HAVING c>1)")
print("  [INFO] 同作者+同正文 重复组数: %d" % n_dup_body)

# ---------- 6. 数据质量：空标题 / 嵌套残留
print("\n-- 数据质量 --")
n_empty_title = one("SELECT COUNT(*) FROM poems WHERE TRIM(title)=''")
chk("无空标题（宋词已回退取首句）", n_empty_title == 0, "空标题 %d 条" % n_empty_title)

# 只判「dict 被 str() 之后的产物」：{'chapter': ...} / "paragraphs"。
# 注意不能只看 '{'——源库本身用 {𥫗/戢} 这种「两个部件拼一个缺字」的记法，那是原样保留的。
n_brace = one("SELECT COUNT(*) FROM poems p WHERE p.body LIKE ? OR p.body LIKE ? "
              "OR p.body LIKE ?", "%'chapter'%", "%paragraphs%", "%'content'%")
chk("正文无嵌套 JSON 残留（蒙学已 flatten）", n_brace == 0, "残留 %d 条" % n_brace)

n_blank = one("SELECT COUNT(*) FROM poems WHERE TRIM(body)=''")
chk("无空正文", n_blank == 0, "%d 条" % n_blank)

# ---------- 7. 抽样展示
print("\n-- 抽样 --")
for dy, kind, label in (("song", "ci", "宋词"), ("unknown", "classic", "蒙学"),
                        ("han", "poem", "曹操"), ("qing", "prose", "幽梦影"),
                        ("song", "fu", "歌赋·宋"), ("han", "fu", "歌赋·汉"),
                        ("qing", "poem", "清代诗词")):
    rows = cur.execute(
        "SELECT a.name, p.title, p.body FROM poems p "
        "LEFT JOIN authors a ON a.id=p.author_id "
        "WHERE (p.id>>27)=? AND ((p.id>>24)&7)=? LIMIT 2",
        (S.DYNASTY[dy], S.KIND[kind])).fetchall()
    for r in rows:
        print("  [%s] 作者=%s 标题=%s" % (label, r[0] if r[0] else "NULL(佚名)", r[1]))
        print("       正文=%s" % (r[2][:60].replace("\n", " / ")))

# 作者缺失分布：区分「真佚名」与「该集合没录作者」
print("\n-- 作者覆盖 --")
rows = cur.execute(
    "SELECT s.name AS src, COUNT(*) c, SUM(CASE WHEN p.author_id=0 THEN 1 ELSE 0 END) nul "
    "FROM poems p JOIN sources s ON s.id=p.src_id GROUP BY s.name ORDER BY c DESC").fetchall()
for r in rows:
    print("  %-18s %7d 条  作者为空 %7d (%.0f%%)"
          % (r["src"], r["c"], r["nul"], 100.0 * r["nul"] / max(r["c"], 1)))

# ---------- 8. 外接集合（歌赋 / 清代诗词 / 毛选…）
print("\n-- 外接集合（external/ 目录） --")
ext_srcs = [r[0] for r in cur.execute(
    "SELECT name FROM sources WHERE name IN ('fu','qing-poetry','mao-xuan')").fetchall()]
print("  已入库: %s" % ("、".join(ext_srcs) if ext_srcs else "（无，说明 external/*.json 未启用）"))
if ext_srcs:
    for r in cur.execute(
            "SELECT s.name, a.name, p.title, p.body FROM poems p "
            "JOIN sources s ON s.id=p.src_id LEFT JOIN authors a ON a.id=p.author_id "
            "WHERE s.name IN ('fu','qing-poetry') LIMIT 5").fetchall():
        print("  [%s] %s 《%s》 %s" % (r[0], r[1] or "佚名", r[2],
                                       r[3][:34].replace("\n", "／")))
    # 外接数据同样要满足简体/无空格，否则等于开了个后门绕过归一
    bad = 0
    for r in cur.execute("SELECT p.body, p.title FROM poems p JOIN sources s "
                         "ON s.id=p.src_id WHERE s.name IN ('fu','qing-poetry')").fetchall():
        for v in (r[0], r[1]):
            if v and (S._zh_convert(v, "zh-cn") != v or any(c in v for c in S._SPACES)):
                bad += 1
                break
    chk("外接数据同样已简体归一、无空格", bad == 0, "异常 %d 条" % bad)

# ---------- 9. 自建号段可用（GUI / API 新增走这里）
print("\n-- 自建号段 --")
n_custom = one("SELECT COUNT(*) FROM poems WHERE (id>>27)=?", S.DYNASTY["custom"])
print("  custom 号段现有 %d 条（GUI/API 新增会落在这里，与构建期 id 不冲突）" % n_custom)
chk("custom 号段 id 可正常反解", S.parse_id(S.make_id("custom", "poem", S.CUSTOM_SEQ_BASE))
    == ("custom", "poem", S.CUSTOM_SEQ_BASE))

# ---------- 10. 核对状态 sidecar（poetry-checks.db，可选）
print("\n-- 核对状态 sidecar --")
chk_db = _here("poetry-checks.db")
if os.path.exists(chk_db):
    cc = sqlite3.connect(chk_db)
    cc.row_factory = sqlite3.Row
    has = cc.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                     "AND name='checks'").fetchone()[0]
    chk("poetry-checks.db 含 checks 表", has == 1)
    if has:
        n_chk = cc.execute("SELECT COUNT(*) FROM checks").fetchone()[0]
        n_ver = cc.execute("SELECT COUNT(*) FROM checks WHERE verified=1").fetchone()[0]
        cc.execute("ATTACH DATABASE ? AS maindb", (DB,))
        bad_fk = cc.execute(
            "SELECT COUNT(*) FROM checks c LEFT JOIN maindb.poems p ON p.id=c.poem_id "
            "WHERE p.id IS NULL").fetchone()[0]
        print("  核对标注 %d 条，已核对 %d 条，指向不存在诗词 %d 条" % (n_chk, n_ver, bad_fk))
        chk("核对标注都指向存在的诗词", bad_fk == 0, "悬空 %d 条" % bad_fk)
    cc.close()
else:
    print("  [INFO] poetry-checks.db 不存在（尚未标记任何核对状态，首次标记时自动创建）")

print("\n" + "=" * 78)
print("总体: " + ("全部通过" if ok else "存在失败项"))
print("=" * 78)
sys.exit(0 if ok else 1)
