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
    """取单个标量。行不存在时返回 None，而不是抛 TypeError。

    why 要容忍空行：meta 里的键是**按需写**的（例如 notes_migrated 只在真的
    迁过注释时才写），拿一个"这次没写"的键去 fetchone()[0] 会让整段校验在
    取数时就崩掉，看起来像库坏了，其实只是这个键不该存在。
    """
    row = cur.execute(sql, a).fetchone()
    return row[0] if row else None


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
print("  poems %d / authors %d / rhythmics %d / genre(体裁) %d / sources %d" % (
    n_poem, one("SELECT COUNT(*) FROM authors"), one("SELECT COUNT(*) FROM rhythmics"),
    one("SELECT COUNT(*) FROM genre"), one("SELECT COUNT(*) FROM sources")))

# ---------- 1b. 体裁字典：genre（schema 3.5 起，3.7 后是唯一的字典表）
# 体裁是唯一「id 与名称分家」的地方：poems 只存 genre_id，名称只存在 genre 表。
# 3.7 删掉了 source（朝代表）与 poems.genre / dynasty_id，这里也顺手断言它们
# 确实不在了——留着会诱使调用方继续往冗余列上写，等于把刚清理的债又借回来。
print("\n-- 体裁字典：genre --")
_n_genre = one("SELECT COUNT(*) FROM genre")
chk("genre 表是体裁字典（%d 行）" % len(S.GENRES), _n_genre == len(S.GENRES),
    "实际 %s 行" % _n_genre)
_got = [tuple(r) for r in cur.execute("SELECT id, name FROM genre ORDER BY id")]
_want = [(g[0], g[2]) for g in S.GENRES]
chk("genre 的 id/名称与 schema.GENRES 完全一致", _got == _want, "实际 %s" % (_got,))
_n_bad_g = one("SELECT COUNT(*) FROM poems WHERE genre_id NOT IN (SELECT id FROM genre)")
chk("每条诗都有合法 genreId（无 0 / 越界）", _n_bad_g == 0, "%d 条越界" % _n_bad_g)
chk("没有 source 表（v3.7 已删，朝代由 id 高位反解）",
    one("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='source'") == 0)
_pcols = [r[1] for r in cur.execute("PRAGMA table_info(poems)")]
chk("poems 没有 dynasty_id 列（v3.7 已删）", "dynasty_id" not in _pcols)
chk("poems 没有 genre 列（v3.7 已删，名称走 genre 表）", "genre" not in _pcols)
for _gid, _name, _c in cur.execute(
        "SELECT g.id, g.name, COUNT(p.id) FROM genre g "
        "LEFT JOIN poems p ON p.genre_id=g.id GROUP BY g.id ORDER BY g.id").fetchall():
    print("  %d %-3s %7d 条 (%.1f%%)"
          % (_gid, _name, _c, 100.0 * _c / max(n_poem, 1)))

# 朝代虽然不落列，但 id 高位必须落在已知编码内（0..13 / 15），
# 否则 parse_id 会反解出一个不存在的朝代，外部按 id 分段就会漏数据。
print("\n-- 朝代（由 id 高位反解，无存储列）--")
_known = ",".join(str(c) for c in sorted(S.DYNASTY.values()))
_n_bad_d = one("SELECT COUNT(*) FROM poems WHERE (id >> %d) NOT IN (%s)"
               % (S.KIND_BITS + S.SEQ_BITS, _known))
chk("每条诗的 id 高位都是已知朝代编码", _n_bad_d == 0, "%d 条越界" % _n_bad_d)
for _dc, _c in cur.execute("SELECT (id >> %d) AS d, COUNT(*) FROM poems "
                           "GROUP BY d ORDER BY d" % (S.KIND_BITS + S.SEQ_BITS)).fetchall():
    print("  %2d %-5s %7d 条"
          % (_dc, S.DYNASTY_LABEL.get(S.DYNASTY_NAME.get(_dc), "未知"), _c))

# authors 是唯一**硬存朝代**的表（authors.id 不含朝代编码，只能靠列），
# 所以这一列的取值必须闭合在 schema.DYNASTY 的键里，否则：
#   ① query.py 用 DYNASTY_LABEL 反解会原样吐出 'chuci' 这种非朝代串；
#   ② 将来若建朝代字典表并加外键，这行数据会直接违约。
# 2026-10-20 之前 sources.py 把三个 AuthorSource 的来源名（chuci/nalan/mengxue）
# 误当 dynasty 传进来，15 位作者因此带着非法朝代值，而这里当时没有断言。
print("\n-- 朝代（authors 硬存列，须闭合在 DYNASTY 编码内）--")
_n_bad_ad = one("SELECT COUNT(*) FROM authors WHERE dynasty NOT IN (%s)"
                % ",".join("?" * len(S.DYNASTY)), *sorted(S.DYNASTY))
chk("authors.dynasty 全部落在 schema.DYNASTY 内", _n_bad_ad == 0,
    "%d 行非法" % _n_bad_ad if _n_bad_ad else "")
for _ac, _c in cur.execute("SELECT dynasty, COUNT(*) FROM authors GROUP BY dynasty "
                           "ORDER BY 2 DESC").fetchall():
    print("  %-10s %-5s %6d 位"
          % (_ac, S.DYNASTY_LABEL.get(_ac, "!! 非法"), _c))

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
    "LEFT JOIN authors a ON a.uid=p.author_uid "
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

n_null = one("SELECT COUNT(*) FROM poems WHERE author_uid=''")
chk("poems 中 author_uid=''（即佚名）已置空", n_null > 0,
    "佚名 %d 条 (%.1f%%)" % (n_null, 100.0 * n_null / max(n_poem, 1)))

# 反查：author_uid='' 的行，JOIN 后 author 必须是 None（空串不该命中任何作者行）
r = cur.execute("SELECT COUNT(*) FROM poems p LEFT JOIN authors a ON a.uid=p.author_uid "
                "WHERE p.author_uid='' AND a.name IS NOT NULL").fetchone()[0]
chk("author_uid='' 行 JOIN 后为 NULL", r == 0, "异常 %d 行" % r)

# 作者名不含"氏"的误伤检查（王氏/花蕊夫人徐氏 是真实姓名）
n_shi = one("SELECT COUNT(*) FROM authors WHERE name LIKE '%氏'")
print("  [INFO] 保留的 X氏 姓名（王氏/花蕊夫人徐氏等）: %d" % n_shi)

print("\n-- 要求3：减少重复数据 --")
n_title_uniq = one("SELECT COUNT(DISTINCT title) FROM poems")
print("  标题: %d 条记录 / %d 个唯一值，重复率 %.2f 次/值（<2 时不值得建字典表）"
      % (n_poem, n_title_uniq, n_poem / max(n_title_uniq, 1)))
for col, label in (("author_uid", "作者"), ("rhythmic_id", "词牌"),
                   ("src_id", "来源"), ("genre_id", "体裁")):
    u = one("SELECT COUNT(DISTINCT %s) FROM poems" % col)
    print("  %-4s: %d 条记录 / %d 个唯一值，重复率 %.1f 次/值 -> 已字典化"
          % (label, n_poem, u, n_poem / max(u, 1)))

# 正文重复度（同一作者+正文出现多次 = 可进一步去重）
n_dup_body = one("SELECT COUNT(*) FROM (SELECT author_uid, body, COUNT(*) c FROM poems "
                 "GROUP BY author_uid, body HAVING c>1)")
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

# ---------- 7. 作者稳定标识（schema 3.2；3.4 起它是主表唯一的作者关联列）
# authors.id 是导入顺序的序号，源库增删一个作者就会整体位移；authors.uid 是由
# (姓名, 朝代) 派生的稳定 uuid。poems.author_uid 必须能落到某一行 authors 上——
# 一旦落空，这首诗在按作者检索 / 详情页上就成了"佚名"，而正文还好端端地在那里，
# 属于最难发现的一类脏数据。
print("\n-- 作者稳定标识（authors.uid / poems.author_uid） --")
# v3.4 的关键结构断言：主表里**只有** author_uid，不再并存一个会位移的序号副本。
_poem_cols = [r[1] for r in cur.execute("PRAGMA table_info(poems)")]
chk("poems 已无 author_id 列（v3.4）", "author_id" not in _poem_cols,
    "仍有该列" if "author_id" in _poem_cols else "列: %s" % ", ".join(_poem_cols))

n_a_uid = one("SELECT COUNT(*) FROM authors WHERE uid=''")
chk("authors.uid 全部非空", n_a_uid == 0, "%d 行空" % n_a_uid)
n_a_dup = one("SELECT COUNT(*) FROM (SELECT uid FROM authors GROUP BY uid HAVING COUNT(*)>1)")
chk("authors.uid 唯一", n_a_dup == 0, "%d 组重复" % n_a_dup)

n_a_bad = one("SELECT COUNT(*) FROM authors WHERE uid NOT LIKE "
              "'________-____-____-____-____________'")
chk("authors.uid 都是 uuid 形态", n_a_bad == 0, "%d 行不是" % n_a_bad)

n_orphan = one("SELECT COUNT(*) FROM poems p LEFT JOIN authors a ON a.uid=p.author_uid "
               "WHERE p.author_uid<>'' AND a.id IS NULL")
chk("有 author_uid 的诗都能关联到作者行", n_orphan == 0, "%d 行落空" % n_orphan)

# n_poems 是"作者名下的作品数"，被 UI 与按作者聚合的下游直接展示；
# 它由 build / store 各自维护，一旦对不上就是某条写路径漏了计数。
n_cnt_bad = one("SELECT COUNT(*) FROM ("
                "SELECT a.uid, a.n_poems, "
                "(SELECT COUNT(*) FROM poems p WHERE p.author_uid=a.uid) c "
                "FROM authors a) WHERE n_poems<>c")
chk("authors.n_poems 与该作者实际作品数一致", n_cnt_bad == 0, "%d 行不符" % n_cnt_bad)

n_a_uid_used = one("SELECT COUNT(DISTINCT author_uid) FROM poems WHERE author_uid<>''")
print("  被诗词引用的作者标识 %d 个 / authors 表 %d 行"
      % (n_a_uid_used, one("SELECT COUNT(*) FROM authors")))

# ---------- 8. extras 落库（schema 3.2 的 poem_extras 表）
# extras.jsonl 是权威源，poem_extras 是它（加上构建期源语料的注释）重放出来的
# 只读镜像；这里校验的是"镜像有没有落对、poems 那一列是不是真的删干净了"。
print("\n-- extras 落库（poem_extras） --")
import extras_db as XDB
for good, name, detail in XDB.check(con):
    chk(name, good, detail)
if XDB.has_table(con):
    st = XDB.stats(con)
    print("  poem_extras %d 行：译文 %d / 注释 %d / 简介 %d / 背景 %d / 赏析 %d / 配图 %d"
          % (st["rows"], st["translation"], st["notes"], st["introduction"],
             st["creative_background"], st["appreciation"], st["images"]))
    print("  图片引用 %d 处；updated（Unix 秒）%s ~ %s"
          % (st["image_refs"], XDB.X.from_epoch(st["updated_min"]) or "-",
             XDB.X.from_epoch(st["updated_max"]) or "-"))
    for k in ("extras_rows", "extras_synced_at", "extras_orphans", "notes_migrated"):
        v = one("SELECT v FROM meta WHERE k=?", k)
        print("  meta.%s = %s" % (k, v if v is not None else "(缺)"))
    chk("meta 记录了 extras 同步信息", one("SELECT v FROM meta WHERE k='extras_rows'") is not None)

# ---------- 9. 抽样展示
print("\n-- 抽样 --")
for dy, kind, label in (("song", "ci", "宋词"), ("unknown", "classic", "蒙学"),
                        ("han", "poem", "曹操"), ("qing", "prose", "幽梦影"),
                        ("song", "fu", "歌赋·宋"), ("han", "fu", "歌赋·汉"),
                        ("qing", "poem", "清代诗词"), ("yuan", "poem", "元诗")):
    rows = cur.execute(
        "SELECT a.name, p.title, p.body FROM poems p "
        "LEFT JOIN authors a ON a.uid=p.author_uid "
        "WHERE (p.id>>27)=? AND ((p.id>>24)&7)=? LIMIT 2",
        (S.DYNASTY[dy], S.KIND[kind])).fetchall()
    for r in rows:
        print("  [%s] 作者=%s 标题=%s" % (label, r[0] if r[0] else "NULL(佚名)", r[1]))
        print("       正文=%s" % (r[2][:60].replace("\n", " / ")))

# 作者缺失分布：区分「真佚名」与「该集合没录作者」
print("\n-- 作者覆盖 --")
rows = cur.execute(
    "SELECT s.name AS src, COUNT(*) c, SUM(CASE WHEN p.author_uid='' THEN 1 ELSE 0 END) nul "
    "FROM poems p JOIN sources s ON s.id=p.src_id GROUP BY s.name ORDER BY c DESC").fetchall()
for r in rows:
    print("  %-18s %7d 条  作者为空 %7d (%.0f%%)"
          % (r["src"], r["c"], r["nul"], 100.0 * r["nul"] / max(r["c"], 1)))

# ---------- 10. 外接集合（歌赋 / 清代诗词 / 毛选…）
print("\n-- 外接集合（external/ 目录） --")
ext_srcs = [r[0] for r in cur.execute(
    "SELECT name FROM sources WHERE name IN ('fu','qing-poetry','mao-xuan')").fetchall()]
print("  已入库: %s" % ("、".join(ext_srcs) if ext_srcs else "（无，说明 external/*.json 未启用）"))
if ext_srcs:
    for r in cur.execute(
            "SELECT s.name, a.name, p.title, p.body FROM poems p "
            "JOIN sources s ON s.id=p.src_id LEFT JOIN authors a ON a.uid=p.author_uid "
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

# ---------- 11. 自建号段可用（GUI / API 新增走这里）
print("\n-- 自建号段 --")
n_custom = one("SELECT COUNT(*) FROM poems WHERE (id>>%d)=?" % (S.KIND_BITS + S.SEQ_BITS),
                S.DYNASTY["custom"])
print("  custom 号段现有 %d 条（GUI/API 新增会落在这里，与构建期 id 不冲突）" % n_custom)
chk("custom 号段 id 可正常反解", S.parse_id(S.make_id("custom", "poem", S.CUSTOM_SEQ_BASE))
    == ("custom", "poem", S.CUSTOM_SEQ_BASE))

# ---------- 12. 核对状态 sidecar（poetry-checks.db，可选）
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
