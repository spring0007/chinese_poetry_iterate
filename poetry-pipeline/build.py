# -*- coding: utf-8 -*-
"""
构建管线：chinese-poetry 源库 -> 统一格式产物。

产物分三块，共用同一套规范化中间层：
  1. poetry.db            SQLite 主库          —— 服务端 / 桌面 / iOS / Android / Flutter
                          （含 poem_extras 表：译文/注释/简介/创作背景/赏析/配图）
  2. bundle/              JSONL 分片 + zstd     —— Web / 小程序 / 离线包 / CDN 按需拉取
  3. poetry-strains.db   平仄可选包（--with-strains 才出）
     + bundle-strains/   平仄分片

平仄为什么独立：它只覆盖唐诗/宋诗（311,594 条 = 全库 89.4%），宋词/蒙学一律没有。
主包带上它要为所有用不到平仄的场景多传一份字节，也让主库无法按"要不要平仄"分别分发。

用法:
    python build.py --out ./dist                  # 主库 + bundle
    python build.py --out ./dist --db-only
    python build.py --out ./dist --bundle-only
    python build.py --out ./dist --with-strains   # 额外出平仄包（db + 分片）
    python build.py --out ./dist --strains-only   # 只出平仄包
    python build.py --out ./dist --with-optional  # 额外打包御定全唐詩（异文版本）
    python build.py --out ./dist --no-external    # 忽略 external/ 下的外接数据
    python build.py --out ./dist --no-extras      # 不把 extras.jsonl 落成 poem_extras 表
                                                  # （注释仍写回 JSONL；这种库读不到注释）
    python build.py --out ./dist --with-fts       # 建 FTS5（体积 x4.2，默认关）
    python build.py --out ./dist --limit 3000     # 冒烟：每个集合只取前 N 条

依赖: Python 3.11+ / zstandard==0.23.0 / zhconv==1.4.3
"""

from __future__ import annotations
import os, sys, json, time, sqlite3, hashlib, shutil, argparse, collections
from typing import Dict, List, Tuple, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema as S
from schema import Poem, make_id, make_uid, make_author_uid, parse_id, pack_strains, quantize_score
import sources as SRC
import extras_db as XDB
import author_heat
import poet_tiers

# 产物默认落在工程目录下的 dist/，按脚本位置解析而非工作目录
# （PyCharm 里工作目录可能与工程根不一致，写 "./dist" 会建到意想不到的地方）。
HERE = os.path.dirname(os.path.abspath(__file__))

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")

try:
    import zstandard as zstd
except ImportError:
    sys.exit("缺少依赖：pip install zstandard==0.23.0")

LOG_WARN: List[str] = []


def warn(msg: str):
    LOG_WARN.append(msg)
    print("  [WARN] " + msg)


# ============================================================ 1. 读取与规范化
def build_poems(colls: List[SRC.Collection], limit: int = 0) -> List[Poem]:
    poems: List[Poem] = []
    seq_counter: Dict[Tuple[str, str], int] = collections.Counter()

    for coll in colls:
        n_in = n_bad = 0
        t0 = time.time()
        for path, rec in SRC.iter_collection(coll):
            n_in += 1
            if limit and n_in > limit:
                break
            lines = SRC.get_body(rec, coll)
            if not lines or not any(l.strip() for l in lines):
                n_bad += 1
                continue
            dynasty = coll.dynasty
            kind = coll.kind
            # 外接集合允许逐条覆盖：歌赋横跨汉/唐/宋，硬拆成三个文件反而难维护。
            # 只对外接数据开放——源库集合的朝代是目录结构决定的，不该被单条记录改写。
            if coll.external:
                dynasty = rec.get("dynasty") or dynasty
                kind = rec.get("kind") or kind
                if dynasty not in S.DYNASTY:
                    raise ValueError("%s：未知 dynasty=%r" % (coll.pattern, dynasty))
                if kind not in S.KIND:
                    raise ValueError("%s：未知 kind=%r" % (coll.pattern, kind))
            seq = seq_counter[(dynasty, kind)]
            seq_counter[(dynasty, kind)] = seq + 1

            extra = {}
            for k in coll.extra:
                if k in rec and rec[k]:
                    extra[k] = rec[k]
            tags = rec.get(coll.tags) if coll.tags else None
            notes = rec.get(coll.notes) if coll.notes else None
            author = ""
            if coll.author:
                author = (rec.get(coll.author) or "").strip()
            if not author:
                author = (rec.get("author") or "").strip()
            if not author:
                author = coll.default_author

            # 体裁 id（v3.5 起，落 poems.genre_id，字典表是 SQLite 的 genre）：
            #   1) 记录里带 genreId 的（八个朝代文件夹已逐条标注）→ 以它为准；
            #   2) 否则用集合级默认（楚辞=骚、纳兰词=词 这类 kind 表达不出来的）；
            #   3) 再否则按 kind 推（全唐诗=诗、宋词=词、蒙学=文…）。
            # 这一步是「写入数据库时统一加同一个 genreId」的落点：其余文件夹的源
            # 文件一个字节都不用改。
            genre_id = rec.get("genreId")
            if not isinstance(genre_id, int) or genre_id not in S.GENRE_NAME:
                genre_id = coll.genre or S.KIND_GENRE.get(kind, 0)

            poems.append(Poem(
                id=make_id(dynasty, kind, seq),
                dynasty=dynasty, kind=kind,
                title=SRC.pick_title(rec, coll, lines),
                author=author,
                lines=lines,
                genre_id=genre_id,
                rhythmic=(rec.get(coll.rhythmic) or "").strip() if coll.rhythmic else "",
                tags=[str(t) for t in tags] if isinstance(tags, list) else ([str(tags)] if tags else []),
                notes=[str(t) for t in notes] if isinstance(notes, list) else ([str(notes)] if notes else []),
                src_id=(rec.get("id") or "") if coll.has_id else "",
                src=coll.name,
                extra=extra,
            ))
        print("  %-16s 读入=%-7d 空正文丢弃=%-4d 用时 %.1fs"
              % (coll.name, min(n_in, limit) if limit else n_in, n_bad, time.time() - t0))
    return poems


# ============================================================ 2. 平仄挂接
def attach_strains(poems: List[Poem], colls: List[SRC.Collection]):
    """strains/ 按 UUID 关联。未命中的保持 None（不是错误：宋词/蒙学本就没有平仄）。"""
    by_src = collections.defaultdict(list)
    for p in poems:
        if p.src_id:
            by_src[p.src].append(p)

    for coll in colls:
        pattern = SRC.STRAIN_MAP.get(coll.name)
        if not pattern:
            continue
        targets = by_src.get(coll.name, [])
        if not targets:
            continue
        idx = {p.src_id: p for p in targets}
        files = SRC.glob_shards(pattern)
        hit = 0
        for path in files:
            for rec in SRC.load_json(path):
                p = idx.get(rec.get("id"))
                if p is None:
                    continue
                ss = rec.get("strains") or []
                joined = "".join(ss)
                if joined:
                    p.strains = pack_strains(joined)
                    p.strains_len = len(joined)
                    hit += 1
        print("  平仄挂接 %-16s 目标=%-7d 命中=%-7d (%.1f%%)"
              % (coll.name, len(targets), hit, 100.0 * hit / max(1, len(targets))))


# ============================================================ 3. 热度挂接
# 热度评估的权威产物：rank/heat-scores.json（扁平 {uid: 综合分 1-100}，2026-10-16 起重评）。
# 评分规则/权重/星级阈值见 rank/heat-meta.json；富明细归档于 poetry-pipeline/dist/backups/。
# 旧五引擎原始快照（rank/poet、rank/ci 分片）已过期，仅在没有该文件时兜底。
HEAT_FILE = os.path.join(os.path.dirname(HERE), "rank", "heat-scores.json")


def _attach_heat_json(poems: List[Poem]) -> int:
    """主路径：heat-scores.json 按 uid 精确挂接。

    why uid 而不是 (author,title)：rank 原始快照既没有 id 也没有正文，旧构建只能拿
    (作者, 题名/词牌) 字符串关联——同作者同题名的多首作品共享一个值，还可能挂错。
    heat-scores.json 是重评产物，直接以 poems.uid 为键，关联精确且随内容稳定。

    文件格式（2026-10-16 起）：扁平 {uid: 综合分(1-100)}，uid 字典序每行一条；
    兼容旧嵌套 {meta, scores:{uid:{...,"c":分}}}（取 c）。规则见 rank/heat-meta.json。

    调用时机硬约束：必须在 normalize_all + dedup **之后**。make_uid 用的是归一
    （简体/去空格）后的作者、标题、正文，提前算出来的 uid 与落库值不一致，会全部
    挂空。旧实现挂在第 2 步（归一前）正是只能用字符串键的原因之一。
    """
    with open(HEAT_FILE, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "scores" in data:
        heat = {uid: rec["c"] for uid, rec in data["scores"].items()}  # 旧嵌套格式
    else:
        heat = data                                             # 扁平 {uid: score}
    hit = 0
    for p in poems:
        score = heat.get(make_uid(p.dynasty, p.author, p.title, p.body))
        if score is not None:
            p.score = int(score)
            hit += 1
    print("  热度挂接 %-20s 命中=%-7d / %d (%.1f%%)  [键=uid]"
          % ("heat-scores.json", hit, len(poems), 100.0 * hit / max(1, len(poems))))
    if hit == 0:
        warn("heat-scores.json 未命中任何记录：uid 与当前构建不一致？"
             "（该文件应为同一套语料上重评的产物）")
    return hit


def _rank_key(rec: dict, kind: str) -> tuple:
    """rank/poet 用 (author,title)；rank/ci 只有 (author,rhythmic)。"""
    if kind == "ci":
        return (rec.get("author", ""), rec.get("rhythmic", ""))
    return (rec.get("author", ""), rec.get("title", ""))


def _attach_rank_shards(poems: List[Poem], colls: List[SRC.Collection]):
    """旧路径（兼容）：没有 heat-scores.json 时，退回五引擎原始快照的字符串关联。

    rank/ 没有 id。曾经试过按分片序号位置对齐，实测只能命中 3.5%——
    rank 是按旧快照抓的，与当前库顺序早就对不上。
    因此只用内容键连接：(author,title) 对诗，(author,rhythmic) 对词。
    键冲突（同一作者同名词作）取最大值，并在覆盖率不足时告警。
    不静默挂错——热度挂到错误的诗上，比没有热度更糟。
    """
    by_src = collections.defaultdict(list)
    for p in poems:
        by_src[p.src].append(p)

    for coll in colls:
        pattern = SRC.RANK_MAP.get(coll.name)
        if not pattern:
            continue
        targets = by_src.get(coll.name, [])
        if not targets:
            continue

        rmap = collections.defaultdict(list)
        for rf in SRC.glob_shards(pattern):
            for rk in SRC.load_json(rf):
                vals = [rk.get(k, 0) or 0 for k in ("baidu", "bing", "bing_en", "google", "so360")
                        if isinstance(rk.get(k, 0), int)]
                rmap[_rank_key(rk, coll.kind)].append(quantize_score(vals))

        hit = 0
        for p in targets:
            key = (p.author, p.rhythmic) if coll.kind == "ci" else (p.author, p.title)
            vs = rmap.get(key)
            if vs:
                p.score = max(vs)
                hit += 1
        total = len(targets)
        print("  热度挂接 %-16s 目标=%-7d 命中=%-7d (%5.1f%%)  [键=%s]"
              % (coll.name, total, hit, 100.0 * hit / max(1, total),
                 "author+rhythmic" if coll.kind == "ci" else "author+title"))
        if hit == 0:
            warn("%s 热度未命中任何记录，全部置 0" % coll.name)
        elif hit < total * 0.9:
            warn("%s 热度覆盖率仅 %.1f%%，排序结果可能不完整"
                 % (coll.name, 100.0 * hit / max(1, total)))


def attach_heat(poems: List[Poem], colls: List[SRC.Collection]):
    """热度挂接入口：优先 heat-scores.json（uid 精确关联），缺失才退回旧快照。"""
    if os.path.exists(HEAT_FILE):
        _attach_heat_json(poems)
    else:
        warn("未找到 %s；退回原始 rank 快照关联（快照已过期，仅作兼容）" % HEAT_FILE)
        _attach_rank_shards(poems, colls)


def merge_poem_authors(authors: List[dict], poems: List[Poem]) -> List[dict]:
    """
    作者 id 字典不能只由作者库文件（authors.tang/song/ci/nantang）构成。

    这几个文件只覆盖唐/宋/五代，汉（曹操）、元（元诗）、清（张潮）以及
    楚辞、蒙学里出现的姓名根本不在里面。只按作者库建映射的话，
    author_uids.get(name) 取不到 → author_uid 落空 → 与"佚名"同义，
    等于把真作者静默抹掉。这是最难发现的一类脏数据：表面上一切正常，
    只有按作者检索时人才不见了。
    """
    known = {(a["name"], a["dynasty"]) for a in authors}
    miss: Dict[Tuple[str, str], str] = {}
    for p in poems:
        k = (p.author, p.dynasty)
        if p.author and k not in known and k not in miss:
            miss[k] = p.dynasty
    for (nm, dyn) in sorted(miss):
        authors.append({"name": nm, "dynasty": dyn, "desc": "", "src": "poem-records"})
    if miss:
        print("  补齐正文出现、作者库缺失的姓名 %d 个，例：%s"
              % (len(miss), "、".join("%s[%s]" % k for k in sorted(miss)[:8])))
    return authors


# ============================================================ 4. 去重 + 标签增强
def normalize_all(poems: List[Poem]):
    """
    全库文本归一：转简体 + 去空格 + 佚名作者置空。
    why 全部转简体而不是保留原文：源库里唐诗/宋诗是繁体、宋词是简体，
    不归一的话用户搜"乡愁"（0 首）和"鄉愁"（40 首）会得到完全不同结果。
    归一之后正文既是展示文本也是检索文本，省掉原先另存一份"简体副本"的开销。
    """
    try:
        from zhconv import convert  # noqa: F401  仅探测可用性
    except ImportError:
        warn("未安装 zhconv==1.4.3，只能去空格、无法转简体，繁体内容将搜不到")

    t0 = time.time()
    n_anon = n_space = 0
    for p in poems:
        old_author = p.author
        p.author = S.norm_author(p.author)
        if old_author and not p.author:
            n_anon += 1
        old_title = p.title
        p.title = S.norm_text(p.title)
        if " " in old_title or "\u3000" in old_title:
            n_space += 1
        p.lines = S.norm_lines(p.lines)
        p.rhythmic = S.norm_text(p.rhythmic)
        p.tags = [S.norm_text(t) for t in p.tags if S.norm_text(t)]
        p.notes = [S.norm_text(t) for t in p.notes if S.norm_text(t)]
        if p.extra:
            p.extra = {k: S.norm_text(v) if isinstance(v, str) else v
                       for k, v in p.extra.items()}
    print("  归一: 佚名置空 %d 条，标题去空格 %d 条，用时 %.1fs"
          % (n_anon, n_space, time.time() - t0))


def dedup(poems: List[Poem]) -> List[Poem]:
    """
    按 **uid** 去重，也就是 (dynasty, author, title, body) 四元组。

    why 必须用 uid 而不是自己再拼一个键：曾经这里拼的是
    `(dynasty, kind, author, title, "".join(lines))`，多了个 `kind`——
    而 `make_uid` 里没有 kind。于是两首诗只要分属不同集合（kind 不同），
    哪怕朝代/作者/标题/正文完全一样，也会被这里放过、却在建唯一索引时撞号。

    2026-09-30 用百度百科补齐 9025 条正文后就真的撞了：同一首诗在两个集合里
    各存一份、原本一份被截断一份完整，补齐之后正文变得一模一样 ⇒
    `CREATE UNIQUE INDEX ix_poems_uid` 直接 IntegrityError，整个构建中断
    （还留下一个 malformed 的 db）。去重键跟 uid 对齐即可，丢掉的那几条
    本来就是内容完全相同的重复项。
    """
    seen = set()
    out = []
    dropped = 0
    samples = []
    for p in poems:
        key = make_uid(p.dynasty, p.author, p.title, p.body)
        if key in seen:
            dropped += 1
            if len(samples) < 5:
                samples.append((p.title, p.author or "佚名", p.kind))
            continue
        seen.add(key)
        out.append(p)
    print("  去重: 保留=%d 丢弃=%d" % (len(out), dropped))
    for t, a, k in samples:
        print("    重复样例 《%s》%s [%s]" % (t, a, k))
    return out


def enrich_tags(poems: List[Poem]):
    """三百首系列带分类标签（咏物/五言律诗/思乡…），主库里没有。
    按 (author, 首行) 匹配回填，匹配不上就跳过——不猜。"""
    overlays = [("全唐诗/唐诗三百首.json", "tang"), ("宋词/宋词三百首.jsonl", "song")]
    idx = collections.defaultdict(list)
    for p in poems:
        idx[(p.author, p.lines[0][:20])].append(p)
    added = 0
    for rel, _dyn in overlays:
        path = os.path.join(SRC.ROOT, rel)
        if not os.path.exists(path):
            continue
        for rec in SRC.load_json(path):
            paras = rec.get("paragraphs") or []
            if not paras:
                continue
            tg = rec.get("tags") or []
            if not tg:
                continue
            for p in idx.get((rec.get("author", ""), paras[0][:20]), []):
                for t in tg:
                    if t not in p.tags:
                        p.tags.append(t)
                added += 1
    print("  标签增强: 回填 %d 条" % added)


# ============================================================ 5. SQLite 产物
def emit_sqlite(poems: List[Poem], authors: List[dict], out_db: str, with_fts: bool,
                with_strains: bool = False):
    if os.path.exists(out_db):
        os.remove(out_db)
    con = sqlite3.connect(out_db)
    # why 8192：正文普遍超过 4KB 页，会走 overflow 链；更大的页能减少溢出页数量。
    con.execute("PRAGMA page_size=8192")
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")

    # 字典表：作者/标题/词牌/来源在全库里大量重复，34 万行各存一遍纯属浪费。
    # 存成 int 后主表每行只剩正文这一个"长字段"，索引也从字符串索引变整数索引。
    # authors.uid 是**稳定标识**：id 是导入顺序的序号（源库多一个作者，后面全体位移），
    # uid 由 (姓名, 朝代) 内容派生，重建不变。挂在作者上的外部引用一律用 uid。
    # score = 作者网络热度综合分（热门作者，rubric 见 rank/author-heat-scores.json）：
    # 其有效评分诗词 poems.score 的算术平均；0 = 名下没有带热度分的诗。
    # 与 poems.score 语义一致（越高越热），单独一列而不是塞进 desc/n_poems。
    con.execute("CREATE TABLE authors("
                "id INTEGER PRIMARY KEY, uid TEXT NOT NULL DEFAULT '', "
                "name TEXT NOT NULL, dynasty TEXT NOT NULL DEFAULT '',"
                "desc TEXT NOT NULL DEFAULT '', n_poems INTEGER NOT NULL DEFAULT 0, "
                "score INTEGER NOT NULL DEFAULT 0)")
    con.execute("CREATE TABLE rhythmics(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)")
    # 两张字典表，名字容易混，一次说清：
    #   genre   —— **体裁表**（id 1..8 = 诗/词/曲/赋/骚/文/新诗/联），
    #              poems.genre_id 指向它（v3.5 起）；名称只存在这里，poems 不抄副本。
    #   sources —— **语料来源集合**（tang-poem / song-ci / user-added…），
    #              poems.src_id 指向它。app.py 靠它反查「这条诗出自哪个 JSON」并把
    #              改动写回源文件，snapshot_user_data 靠它认出 user-added/user-edit，
    #              所以这张表不能删。
    # v3.7 删掉了 `source`（朝代表）：朝代是 id 高 4 位的编码，`id >> 27` 就能反解，
    # 再存一张表加一列 dynasty_id 属于同一事实抄两遍，已整体回退。
    con.execute("CREATE TABLE sources(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)")
    con.execute("CREATE TABLE genre(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)")
    # 体裁字典写死：它不进 _tid 那套「按出现顺序编号」的逻辑——那种编号会被集合
    # 顺序影响，而 genreId 是要写进源数据、被外部按 id 过滤的，必须与 schema.GENRES
    # 逐字一致。
    con.executemany("INSERT INTO genre VALUES(?,?)",
                    [(gid, name) for gid, _code, name, _desc in S.GENRES])

    # 主表不存 dynasty/kind：id 高位已编码（见 schema.make_id），查朝代用
    # `id >> 27` 配 schema.DYNASTY_LABEL 反解即可，v3.7 起不再落 dynasty_id 列。
    # title 直接存字符串，不进字典表——实测 273,134 个唯一标题 / 345,354 首诗，
    # 重复率只有 1.3 次/值，字典表省不下数据，却要多付一份表 + 唯一索引 + 普通索引
    # 共 29.3 MB；逐行存只要 2.71 MB。字典化只对高重复列（作者 25.7 次/值、
    # 词牌 236 次/值、来源 23024 次/值）才划算。
    # 作者关联只走 author_uid（v3.4 起）：它是 authors.uid 的伴生列，由 (姓名, 朝代)
    # 内容派生，重建不变；佚名的 author_uid=''（join 不上任何作者行，等价于 NULL）。
    # why 不再同时存 author_id：那一列是 authors 的**导入顺序序号**，源库增删一位
    # 作者、后面全体位移，于是每次重建都要在 34 万行主表上重写一遍这一列 + 重建
    # 它的索引（实测 ix_poems_author 约 1.8 MB），换来的信息量为零——
    # authors.id 本来就是 authors 表内部的主键，主表持有一份副本只会制造
    # "两份作者标识谁说了算"的歧义（v3.2 两列并存时，两者一旦不同步就是静默挂错人）。
    con.execute("""
        CREATE TABLE poems(
            id INTEGER PRIMARY KEY,
            uid TEXT NOT NULL,
            author_uid TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            rhythmic_id INTEGER NOT NULL DEFAULT 0,
            src_id INTEGER NOT NULL DEFAULT 0,
            genre_id INTEGER NOT NULL DEFAULT 0,
            body TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '',
            score INTEGER NOT NULL DEFAULT 0,
            n_char INTEGER NOT NULL DEFAULT 0
        )""")
    # v3.3 起 poems 没有 notes 列：注释的唯一存放处是 poem_extras.notes，构建期
    # 从源语料直接写进去（见下面 notes_map 与 main 第 5.5 步）。why 要删掉这一列
    # 而不是留着不用：留着就永远有"两处都能写"的可能，而 v3.2 的返工正是踩在
    # "先落主库、再搬一次"这条往返链路上——同一条数据过两次手就必然会有一次丢。
    # 平仄不在这里建表：它是可选包（见 emit_strains）。主库保持"只有检索必需的东西"。
    # extras 落库表在这里只建结构，内容由 extras_db.sync() 灌（见 main 第 5.5 步）。
    XDB.create_table(con)

    # 作者 (姓名, 朝代) -> 稳定 uid。uid 按 (姓名, 朝代) 算，与 authors 行一一对应
    # （同名跨朝代是不同的人，实测 330 组，见 schema.make_author_uid）。
    # 键必须是 (name, dynasty) 二元组，不能只用 name：同名跨朝代（如 严武 在唐宋、
    # 曹植 在魏晋/三国、陶渊明 在魏晋/晋 各一条）时，name 单键会"后者覆盖前者"，
    # 让某个朝代的同名作者行变成 n_poems=0 的空壳、其诗被错误挂到另一个朝代名下。
    # 诗的 author_uid 用 (p.author, p.dynasty) 查，保证每首诗挂到与自己同朝代的作者行。
    author_uids = {(a["name"], a["dynasty"]): make_author_uid(a["name"], a["dynasty"])
                   for a in authors}
    rhythmic_ids: Dict[str, int] = {}
    src_ids: Dict[str, int] = {}

    def _tid(d: Dict[str, int], name: str) -> int:
        if not name:
            return 0
        v = d.get(name)
        if v is None:
            v = len(d) + 1
            d[name] = v
        return v

    rows = []
    # uid -> 注释文本。v3.3 起 poems 不存注释，这批内容由 5.5 步交给 extras_db：
    # 先写回权威源 extras.jsonl，再随 JSONL 一起重放进 poem_extras。
    notes_map: Dict[str, str] = {}
    for p in poems:
        uid = make_uid(p.dynasty, p.author, p.title, p.body)
        rows.append((
            p.id,
            uid,
            author_uids.get((p.author, p.dynasty), ""),
            p.title,
            _tid(rhythmic_ids, p.rhythmic),
            _tid(src_ids, p.src),
            p.genre_id,
            p.body,
            "\x1f".join(p.tags),
            p.score,
            p.n_char,
        ))
        if p.notes:
            notes_map[uid] = "\n".join(p.notes)

    lost = sum(1 for p in poems if p.author and not author_uids.get((p.author, p.dynasty)))
    if lost:
        warn("%d 条记录有作者名却拿不到 author_uid，关联作者时会显示为佚名" % lost)

    con.executemany(
        "INSERT INTO poems(id,uid,author_uid,title,rhythmic_id,src_id,"
        "genre_id,body,tags,score,n_char) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)

    # n_poems 按"指向本行的作品数"统计，而不是按姓名统计。
    # why 按姓名会虚高：同名可跨朝代并存（实测 275 组，如唐宋各一个「严武」），
    # 而 author_uids 是 {姓名: 最后一条} 的"后者覆盖"，所以唐朝那个「李煜」行
    # 一条作品都不指向它，按姓名却会分到 111 条（那是南唐李煜的作品数）。
    # 口径必须与 store.py 的运行期维护方式一致（那里就是按 authors.id 增减的），
    # verify_db.py 有一条断言盯着"n_poems == 实际作品数"。
    used = collections.Counter()
    for p in poems:
        u = author_uids.get((p.author, p.dynasty), "")
        if u:
            used[u] += 1
    # 作者热度分（热门作者）：调 author_heat.aggregate_author_scores —— 公式全仓
    # 只有一份实现，author_heat.py（读主库写 JSON/DB）与这里（重建时内存聚合）
    # 不会各算各的、慢慢漂移。poems.score 在第 3 步 attach_heat 之后就是最终值；
    # 佚名（author_uid 折成空串）与未获热度分的诗不参与聚合。
    # 算分失败不阻断建库：诗才是主产物，作者分是衍生展示，置 0 后可由
    # author_heat.py --apply 事后补（它从落库后的 poems.score 重新聚合）。
    try:
        aheat = {u: v["score"] for u, v in author_heat.aggregate_author_scores(
            (author_uids.get((p.author, p.dynasty), ""), p.score) for p in poems).items()}
    except Exception as e:
        warn("作者热度分计算失败：%s（authors.score 置 0，可事后跑 author_heat.py --apply 修复）" % e)
        aheat = {}
    # 诗人分级覆盖层（poet_tiers）：34 位人工分级诗人按档位改分（五档分类与
    # 分数一一对应：第一档100 / 第二档98 / 第三档96 / 第四档94 / 第五档92），
    # 覆盖热度聚合值。库中无作者行的 12 位自动跳过（它们只在 rank/poet-tiers.json，
    # 不建作者行）。覆盖失败不阻断建库：诗才是主产物，保留热度聚合分，
    # 可重跑 poet_tiers.py --apply 补齐。
    try:
        n_tier = poet_tiers.apply_overrides(aheat, author_uids)
        if n_tier:
            print("  诗人分级覆盖 %d 位作者分（档位分见 rank/poet-tiers.json）" % n_tier)
    except Exception as e:
        warn("诗人分级覆盖失败：%s（保留热度聚合分，可重跑 poet_tiers.py --apply）" % e)
    author_rows = []
    for i, a in enumerate(authors):
        uid = make_author_uid(a["name"], a["dynasty"])
        author_rows.append((i + 1, uid, a["name"], a["dynasty"], a["desc"],
                            used.get(uid, 0), aheat.get(uid, 0)))
    # 具名列而不是 VALUES(?...) 位置参数：authors 表加过 score 列，位置写法会在
    # 列数变化时静默错位或直接报错；具名列加列只影响愿意传的调用方。
    con.executemany(
        "INSERT INTO authors(id,uid,name,dynasty,desc,n_poems,score) VALUES(?,?,?,?,?,?,?)",
        author_rows)
    con.executemany("INSERT INTO rhythmics VALUES(?,?)", [(v, k) for k, v in rhythmic_ids.items()])
    con.executemany("INSERT INTO sources VALUES(?,?)", [(v, k) for k, v in src_ids.items()])
    # 体裁分布一行速览：建完库立刻能看出「哪一类一条都没落上」（genre_id=0 就是漏了）
    gstat = collections.Counter(p.genre_id for p in poems)
    print("  poems %d 行 / authors %d / rhythmics %d" % (len(rows), len(authors), len(rhythmic_ids)))
    print("  体裁分布 " + "、".join(
        "%s=%d" % (S.GENRE_NAME.get(g, "未标注"), n)
        for g, n in sorted(gstat.items(), key=lambda x: (x[0] or 99))))
    # 朝代不落列，直接从 id 高位反解统计（v3.7）
    print("  朝代分布 " + "、".join(
        "%s=%d" % (S.DYNASTY_LABEL.get(S.DYNASTY_NAME.get(g), "未知"), n)
        for g, n in sorted(collections.Counter(p.id >> (S.KIND_BITS + S.SEQ_BITS) for p in poems).items())))

    # 注意：rhythmics.name / sources.name 已声明 UNIQUE，SQLite 会自动建
    # sqlite_autoindex_*，再手动建一个同名索引就是纯浪费（实测白白多占一份）。
    # uid 是给外部数据（extras 等）关联用的主键，必须唯一；顺带承担点查。
    # why 不直接拿它当 PRIMARY KEY：id 的 int32 高位编码 dynasty/kind 是查询
    # 与分片的基础（见 schema.make_id），uid 只是「对外稳定引用」，两者分工不同。
    con.execute("CREATE UNIQUE INDEX ix_poems_uid ON poems(uid)")
    # 作者只经 author_uid 关联（v3.4）：authors.uid 上有唯一索引，这个 JOIN
    # 走的是 36 字符的文本索引而不是 int 索引，实测检索延迟无感差异，但省掉了
    # 一列会随重建整体位移、每次都要重写与重建索引的副本。
    con.execute("CREATE INDEX ix_poems_author_uid ON poems(author_uid)")
    con.execute("CREATE INDEX ix_poems_title ON poems(title)")
    con.execute("CREATE INDEX ix_poems_rhythmic ON poems(rhythmic_id)")
    # 朝代不建索引（v3.7）：没有 dynasty_id 列了。要按朝代筛就直接写
    # `WHERE id >= lo AND id < hi`（id 高位就是朝代），PK 的 B-tree 天然有序，
    # 反而比一列低基数索引更省——既不多占空间，也不会被优化器误判。
    # 体裁是「按类浏览」的入口（只要词 / 只要赋），低基数但查询必带它，
    # 建索引后是 8 段连续区间，比全表扫 34 万行便宜一个数量级。
    con.execute("CREATE INDEX ix_poems_genre ON poems(genre_id)")
    con.execute("CREATE INDEX ix_poems_score ON poems(score DESC)")
    con.execute("CREATE UNIQUE INDEX ix_authors_uid ON authors(uid)")
    con.execute("CREATE INDEX ix_authors_name ON authors(name)")
    # 作者热度榜（热门作者）与诗词热度榜对称：ORDER BY a.score DESC 走它，
    # 不建就会为了吃掉排序而全表扫 1.4 万行——数据量小、扫描也快，但榜单是
    # 高频入口，没理由每次全扫。 poems 侧的 ix_poems_score 同理。
    con.execute("CREATE INDEX ix_authors_score ON authors(score DESC)")

    # ---- 检索：默认只建普通索引，用 LIKE 查 body ----
    # 实测（345,390 条全量）：
    #   FTS5 trigram  -> 库从 210.9MB 涨到 462.9MB（+252MB），且 <3 字查询命中 0
    #   正文 bigram 倒排 -> 15,021,993 行，库涨到 ~790MB，构建 230s —— 直接否决
    #   LIKE 全表扫描  -> 高频词 4~22ms，未命中 ~1.1s，零额外空间
    # 结论：默认不建 FTS。需要长句毫秒级响应的公开服务才开 --with-fts。
    if with_fts:
        t0 = time.time()
        con.execute("""CREATE VIRTUAL TABLE poem_fts USING fts5(
                         body, id UNINDEXED, tokenize='trigram')""")
        con.execute("INSERT INTO poem_fts(body,id) SELECT body, id FROM poems")
        con.commit()
        print("  FTS5 trigram 构建 %.1fs（注意：不支持 <3 字查询）" % (time.time() - t0))

    con.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO meta VALUES(?,?)", [
        ("schema_version", S.SCHEMA_VERSION),
        ("built_at", time.strftime("%Y-%m-%dT%H:%M:%S")),
        ("n_poems", str(len(poems))),
        ("n_authors", str(len(authors))),
        ("n_rhythmics", str(len(rhythmic_ids))),
        ("n_genres", str(len(S.GENRES))),
        ("text_form", "simplified-nospace"),
        ("fts", "1" if with_fts else "0"),
        ("strains_pkg", "1" if with_strains else "0"),
        ("note", "全库简体归一、无空格。body 兼作检索列，查询词需先转简体。"
                 "dynasty/kind 由 id 高位反解；佚名的 author_uid=''。"
                 "平仄在独立可选包 poetry-strains.db，通过 id 关联。"
                 "uid 是内容派生的稳定 uuid：外部数据（extras）一律用 uid 关联，"
                 "不要用 id —— id 含序号，会因 dedup/重排而整体位移。"
                 "作者关联只走 author_uid（authors.uid 的伴生列，由 姓名+朝代 派生，"
                 "重建不变）：poems 表**没有 author_id 列**（v3.4 移除），"
                 "写法是 LEFT JOIN authors a ON a.uid = p.author_uid。"
                 "译文/注释/简介/背景/赏析/配图在 poem_extras 表（uid 关联）；"
                 "poems 表**没有 notes 列** —— 注释的唯一存放处是 poem_extras.notes，"
                 "构建期直接从源语料写入，不再经过主库中转。"
                 "体裁在 poems.genre_id（字典表 genre(id,name)，1 诗 2 词 3 曲 4 赋 "
                 "5 骚 6 文 7 新诗 8 联），名称从 genre 表 JOIN 出来，poems 里不存副本（v3.7）。"
                 "朝代**没有列**：id 高 4 位就是朝代编码，用 `id >> 27` 配 "
                 "schema.DYNASTY_LABEL 反解，按朝代筛就用 id 区间（v3.7 起）。"
                 "语料来源集合在同目录的 sources 表（复数），和 genre 表职责不同，别混用。"),
    ])
    # ANALYZE 必须有：没有 sqlite_stat1 时，SQLite 会为了吃掉 ORDER BY score DESC
    # 而选 ix_poems_score 做全索引扫描，把 p.title = ? 这种高选择性查询拖到 900ms+。
    # 有统计信息后它会正确选择 ix_poems_title。代价只有几 KB。
    con.execute("ANALYZE")
    con.commit()
    con.execute("VACUUM")
    size = os.path.getsize(out_db)
    con.close()
    # 注释不落本库（poems 没有 notes 列），交给 main 第 5.5 步写进 poem_extras。
    return size, notes_map


# ============================================================ 5a. 用户数据保全
def snapshot_user_data(db_path: str):
    """
    重建前读取现有 poetry.db 里的用户数据（自建 / 修订 + 其作者 / 词牌 / 来源）。
    返回可重放的快照列表；库不存在或主版本不一致时返回 None（不保全，避免跨版本错乱）。

    why 需要这步：build.py 是整库重建，会删掉 poetry.db 从头生成。而自建 / 修订条目
    是唯一没有别处副本的数据（源 JSON 里没有它），不保全就直接丢了。
    核对状态在独立 sidecar（poetry-checks.db），本步不碰它——重建 poetry.db 不影响。
    """
    if not os.path.exists(db_path):
        return None
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
    except sqlite3.OperationalError:
        con.close()
        return None
    if not row or row[0].split(".")[0] != S.SCHEMA_VERSION.split(".")[0]:
        con.close()
        warn("现有库 schema 版本 %s 与当前 %s 不一致，跳过用户数据保全"
             "（请用 export / export-edits 另行备份）" % (row[0] if row else "未知", S.SCHEMA_VERSION))
        return None
    # 注释的来源随版本变过：3.2 之前在主库 poems.notes 列，3.2 起搬到 poem_extras。
    # 两处都探一下，否则"老库里那一列还在、新库里注释在另一张表"必然漏一种，
    # 而漏掉的表现是"重建后用户自己写的注释没了"——静默、无报错。
    has_notes_col = any(r[1] == "notes" for r in con.execute("PRAGMA table_info(poems)"))
    has_ex = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                         "AND name='poem_extras'").fetchone()[0] > 0
    # 作者关联列也随版本变过：v3.4 起 poems 只有 author_uid（author_id 已删）。
    # 老库（≤3.3）两列都在，取 author_uid；再老（≤3.1）只有 author_id，退回它，
    # 否则"用老库重建"会直接报 no such column。
    has_au = any(r[1] == "author_uid" for r in con.execute("PRAGMA table_info(poems)"))
    ajoin = "a.uid = p.author_uid" if has_au else "a.id = p.author_id"
    # 体裁列 v3.5 才有：拿老库（≤3.4）重建时不能 SELECT p.genre_id，否则直接报
    # no such column。老库没有体裁就记 0，恢复时再按 kind 兜底（见 restore_user_data）。
    has_genre = any(r[1] == "genre_id" for r in con.execute("PRAGMA table_info(poems)"))
    genre_expr = "p.genre_id" if has_genre else "0"
    notes_expr = "p.notes" if has_notes_col else "''"
    if has_ex:
        notes_expr = ("COALESCE(NULLIF(%s,''), "
                      "(SELECT notes FROM poem_extras pe WHERE pe.uid=p.uid), '')"
                      % notes_expr)
    rows = con.execute("""
        SELECT p.id, %s AS author_uid, p.title, p.rhythmic_id, p.src_id, p.body, p.tags,
               %s AS notes, p.score, p.n_char, s.name AS src_name,
               %s AS genre_id,
               a.name AS author_name, a.dynasty AS author_dyn,
               r.name AS rhythmic_name
        FROM poems p
        JOIN sources s ON s.id = p.src_id
        LEFT JOIN authors a ON %s
        LEFT JOIN rhythmics r ON r.id = p.rhythmic_id
        WHERE s.name IN ('user-added', 'user-edit')
    """ % ("p.author_uid" if has_au else "''", notes_expr, genre_expr, ajoin)).fetchall()
    out = [dict(x) for x in rows]
    con.close()
    return out


def restore_user_data(con, snapshot) -> tuple:
    """
    把快照重放到新建的 con 上：保留原 id（修订 = 覆盖同 id 的构建期行；
    自建 = custom 号段不冲突）。作者按 author_uid 复用新库那一行（v3.4 起作者关联
    只有这一列；老库快照没有该列时退回按名对齐），词牌 / 来源按名复用，找不到就新建。
    最后用 GROUP BY 重算 authors.n_poems，保证计数一致。

    返回 (恢复条数, {uid: 注释})。注释**不能**直接写回主库（v3.3 起没有那一列），
    而是交回 main 与构建期注释合并后统一走 extras 落库那一步——否则用户自己写的
    注释会在重建时丢掉。
    """
    if not snapshot:
        return 0, {}
    notes_map: Dict[str, str] = {}
    for src in ("user-added", "user-edit"):
        con.execute("INSERT OR IGNORE INTO sources(name) VALUES(?)", (src,))
    # 作者表两张索引并行维护：
    #   by_uid  —— 正常路径。快照里的 author_uid 原样沿用，标识不因重建而变。
    #   by_name —— 老库（≤3.1）的 poems 没有 author_uid，只能按名对齐。
    #              按 id 升序、**后面覆盖前面**，与 emit_sqlite 里
    #              author_uids = {a["name"]: ...} 的"后者覆盖"语义严格一致，
    #              否则同名跨朝代的作者在重建前后会指向不同的行、author_uid 跟着错位。
    by_name, by_uid = {}, {}
    for _name, _aid, _auid in con.execute("SELECT name, id, uid FROM authors ORDER BY id"):
        by_name[_name] = (_aid, _auid)
        if _auid:
            by_uid[_auid] = (_aid, _auid)
    rhythmics = {r[0]: r[1] for r in con.execute("SELECT name, id FROM rhythmics")}
    src_ids = {r[0]: r[1] for r in con.execute("SELECT name, id FROM sources")}
    for p in snapshot:
        aname = p["author_name"]
        auid = p["author_uid"] or ""
        aid = 0
        if auid:
            got = by_uid.get(auid)
            if got is None:
                # 快照带着 author_uid、新库里却没有这位作者（作者那一版一条诗都没被
                # 构建期引用，或作者名/朝代被改过）——补一行，标识原样沿用，
                # 否则这条诗的作者关联会静默断掉。
                cur = con.execute(
                    "INSERT INTO authors(uid,name,dynasty,desc,n_poems) VALUES(?,?,?,?,0)",
                    (auid, aname or "", p["author_dyn"] or "", ""))
                aid = cur.lastrowid
                by_uid[auid] = (aid, auid)
                if aname:
                    by_name[aname] = (aid, auid)
            else:
                aid = got[0]
        elif aname:
            # 老库快照（没有 author_uid）：按名对齐到构建期那一行。
            got = by_name.get(aname)
            if got is None:
                auid = make_author_uid(aname, p["author_dyn"] or "")
                cur = con.execute(
                    "INSERT INTO authors(uid,name,dynasty,desc,n_poems) VALUES(?,?,?,?,0)",
                    (auid, aname, p["author_dyn"] or "", ""))
                aid = cur.lastrowid
                by_name[aname] = (aid, auid)
                by_uid[auid] = (aid, auid)
            else:
                aid, auid = got
        rid = 0
        rn = p["rhythmic_name"]
        if rn:
            rid = rhythmics.get(rn)
            if rid is None:
                cur = con.execute("INSERT INTO rhythmics(name) VALUES(?)", (rn,))
                rid = cur.lastrowid
                rhythmics[rn] = rid
        sid = src_ids[p["src_name"]]
        # INSERT OR REPLACE：修订条目 id 与构建期同 id，直接覆盖那一行；
        # 自建条目 id 落在 custom 号段，不会撞，正常插入。
        # uid 必须跟着写：poems.uid 是 NOT NULL，而且外部数据（extras）靠它关联。
        # dynasty/kind 从 id 高位反解（主表不存这两列，见 schema.parse_id）；
        # 作者名用上面按名对齐后的 aname，保证与构建期同一首诗算出同一个 uid。
        _dyn, _kind, _seq = parse_id(p["id"])
        # v3.3：poems 不再有 notes 列。注释由 notes_map 带出去——5.5 步会先把它
        # 写回权威源 extras.jsonl、再随 JSONL 重放进 poem_extras，所以重建后它还在，
        # 而且这份"在"是可复现的（不会像"只写表不写源"那样被下一次重放抹掉）。
        uid = make_uid(_dyn, aname or "", p["title"], p["body"])
        # 体裁：快照带得出来就照搬（用户在库里改过的话以库里为准）；老库没有这一列
        # 时按 kind 兜底，避免恢复进来的条目 genre_id=0 变成「未标注」。
        # 名称不落库（v3.7 删了 poems.genre），需要时 JOIN genre 表。
        gid = p["genre_id"] if "genre_id" in p.keys() else 0
        if gid not in S.GENRE_NAME:
            gid = S.KIND_GENRE.get(_kind, 0)
        # 朝代不用写（v3.7 删了 dynasty_id）：id 高位就是朝代，_dyn 已经由
        # parse_id 反解出来；修订条目的 id 不变，朝代也一定不变。
        con.execute(
            "INSERT OR REPLACE INTO poems(id,uid,author_uid,title,rhythmic_id,"
            "src_id,genre_id,body,tags,score,n_char) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (p["id"], uid, auid, p["title"], rid, sid, gid,
             p["body"], p["tags"], p["score"], p["n_char"]))
        txt = (p["notes"] or "").replace("\x1f", "\n").strip()
        if txt:
            notes_map[uid] = txt
    con.execute("UPDATE authors SET n_poems=0")
    # 计数按 author_uid 统计（v3.4 起主表没有 author_id 了）；author_uid='' 是
    # 佚名，不指向任何作者行，直接排除。
    counts = con.execute(
        "SELECT author_uid, COUNT(*) c FROM poems WHERE author_uid<>'' "
        "GROUP BY author_uid").fetchall()
    con.executemany("UPDATE authors SET n_poems=? WHERE uid=?", [(c, u) for u, c in counts])
    con.commit()
    return len(snapshot), notes_map


# ============================================================ 5b. 平仄可选包
def emit_strains(poems: List[Poem], out_dir: str, level: int = 12,
                 shard_size: int = 4000, db: bool = True, bundle: bool = True):
    """
    平仄独立产出。两条链路都按 id 与主包一一对应，不做冗余拷贝：
      poetry-strains.db      —— 桌面/服务端：ATTACH 后按 id 取
      bundle-strains/*.zst   —— Web/移动端：按需拉取对应分片

    只有挂接上平仄的记录才落盘（唐诗/宋诗约 60%），空记录不占位。
    """
    items = [(p.id, p.strains, p.strains_len) for p in poems if p.strains]
    if not items:
        warn("没有任何记录挂接上平仄，平仄包为空（检查 strains/ 目录是否存在）")
    print("  平仄记录 %d 条（占全库 %.1f%%）" % (len(items), 100.0 * len(items) / max(1, len(poems))))

    db_size = 0
    if db:
        p = os.path.join(out_dir, "poetry-strains.db")
        if os.path.exists(p):
            os.remove(p)
        con = sqlite3.connect(p)
        con.execute("PRAGMA page_size=8192")
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute("""CREATE TABLE poem_strains(
                         id INTEGER PRIMARY KEY, data BLOB NOT NULL, len INTEGER NOT NULL)""")
        con.executemany("INSERT INTO poem_strains VALUES(?,?,?)", items)
        con.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
        con.executemany("INSERT INTO meta VALUES(?,?)", [
            ("schema_version", S.SCHEMA_VERSION),
            ("package", "strains"),
            ("built_at", time.strftime("%Y-%m-%dT%H:%M:%S")),
            ("n_records", str(len(items))),
            ("encoding", "4bit/symbol; 0=仄 1=平 2=，3=。4=○ 5=？6=通 7=未知"),
            ("note", "通过 id 与主库 poems.id 关联；奇数长度在末字节补 0 nibble，"
                     "解包时按 len 字段截断。"),
        ])
        con.commit()
        con.execute("VACUUM")
        con.close()
        db_size = os.path.getsize(p)
        print("  -> %s  %.2f MB" % (p, db_size / 1048576))

    bundle_size = 0
    if bundle:
        bdir = os.path.join(out_dir, "bundle-strains")
        os.makedirs(os.path.join(bdir, "shards"), exist_ok=True)
        by_grp = collections.defaultdict(list)
        id_to_p = {p.id: p for p in poems}
        for pid, _data, _ln in items:
            pp = id_to_p[pid]
            by_grp[(pp.dynasty, pp.kind)].append(pid)

        manifest = {
            "schema_version": S.SCHEMA_VERSION,
            "package": "strains",
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "compression": "zstd-%d" % level,
            "fields": [{"k": k, "desc": d} for k, d in S.STRAIN_BUNDLE_FIELDS],
            "join": "i 与主包 bundle/shards/*.jsonl.zst 里的 i 一一对应",
            "shards": [], "total_records": len(items), "total_shards": 0,
        }
        n = 0
        for (dy, kind), ids in sorted(by_grp.items()):
            ids.sort()
            for i in range(0, len(ids), shard_size):
                chunk = ids[i:i + shard_size]
                payload = "".join(
                    json.dumps({"i": pid, "p": id_to_p[pid].strains.hex(),
                                "pl": id_to_p[pid].strains_len},
                               ensure_ascii=False, separators=(",", ":")) + "\n"
                    for pid in chunk).encode("utf-8")
                comp = zstd_compress(payload, level)
                name = "%s-%s-%04d.strains.jsonl.zst" % (dy, kind, i // shard_size)
                with open(os.path.join(bdir, "shards", name), "wb") as f:
                    f.write(comp)
                manifest["shards"].append({
                    "file": "shards/" + name, "dynasty": dy, "kind": kind,
                    "records": len(chunk), "bytes": len(comp), "raw_bytes": len(payload),
                    "sha256": hashlib.sha256(comp).hexdigest(),
                    "id_from": chunk[0], "id_to": chunk[-1],
                })
                n += 1
        manifest["total_shards"] = n
        with open(os.path.join(bdir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        bundle_size = sum(s["bytes"] for s in manifest["shards"])
        print("  -> %s  %.2f MB (%d 分片)" % (bdir, bundle_size / 1048576, n))
    return db_size, bundle_size, len(items)


# ============================================================ 6. bundle 产物
def zstd_compress(data: bytes, level: int = 12) -> bytes:
    return zstd.ZstdCompressor(level=level).compress(data)


def emit_bundle(poems: List[Poem], authors: List[dict], out_dir: str, level: int,
                shard_size: int = 2000, with_strains: bool = False):
    # 不做 rmtree：构建产物目录可能有几百个分片文件，整体删除风险高且会被安全策略拦截。
    # 改为覆盖写入 + 仅清理「本次未生成」的陈旧分片，并限制清理数量。
    os.makedirs(os.path.join(out_dir, "shards"), exist_ok=True)
    os.makedirs(os.path.join(out_dir, "index"), exist_ok=True)

    groups = collections.defaultdict(list)
    for p in poems:
        groups[(p.dynasty, p.kind)].append(p)

    manifest = {
        "schema_version": S.SCHEMA_VERSION,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "compression": "zstd-%d" % level,
        "text_form": "simplified-nospace",
        "fields": [{"k": k, "desc": d} for k, d in S.BUNDLE_FIELDS],
        "id_layout": "int32 = (dynasty<<27)|(kind<<24)|seq; 见 index/enums.json",
        "shards": [],
        "total_records": len(poems),
        "total_shards": 0,
    }

    n_shard = 0
    for (dy, kind), plist in sorted(groups.items()):
        plist.sort(key=lambda p: p.id)
        for i in range(0, len(plist), shard_size):
            chunk = plist[i:i + shard_size]
            payload = "".join(json.dumps(p.to_json_obj(with_strains=with_strains),
                                         ensure_ascii=False,
                                         separators=(",", ":")) + "\n"
                              for p in chunk).encode("utf-8")
            comp = zstd_compress(payload, level)
            name = "%s-%s-%04d.jsonl.zst" % (dy, kind, i // shard_size)
            path = os.path.join(out_dir, "shards", name)
            with open(path, "wb") as f:
                f.write(comp)
            manifest["shards"].append({
                "file": "shards/" + name,
                "dynasty": dy,
                "kind": kind,
                "records": len(chunk),
                "bytes": len(comp),
                "raw_bytes": len(payload),
                "sha256": hashlib.sha256(comp).hexdigest(),
                "id_from": chunk[0].id,
                "id_to": chunk[-1].id,
            })
            n_shard += 1
    manifest["total_shards"] = n_shard

    # 作者索引：小（约 1.4 万条），客户端可直接载入做联想
    n_by_author = collections.Counter(p.author for p in poems if p.author)
    aidx = {}
    for i, a in enumerate(sorted(authors, key=lambda x: (x["dynasty"], x["name"]))):
        aidx.setdefault(a["name"], {"id": i, "dynasty": a["dynasty"], "n": n_by_author.get(a["name"], 0)})
    # 标题索引：26 万条，用于标题精确/前缀查
    tidx = collections.defaultdict(list)
    for p in poems:
        if p.title:
            tidx[p.title].append(p.id)
    # 词牌索引
    ridx = collections.defaultdict(list)
    for p in poems:
        if p.rhythmic:
            ridx[p.rhythmic].append(p.id)

    for fname, obj in [
        ("authors.json", {k: v for k, v in sorted(aidx.items())}),
        ("titles.json", {k: v for k, v in sorted(tidx.items())}),
        ("rhythmics.json", {k: v for k, v in sorted(ridx.items())}),
    ]:
        payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        comp = zstd_compress(payload, level)
        with open(os.path.join(out_dir, "index", fname + ".zst"), "wb") as f:
            f.write(comp)
        manifest.setdefault("index", []).append({
            "file": "index/" + fname + ".zst",
            "entries": len(obj),
            "bytes": len(comp),
            "sha256": hashlib.sha256(comp).hexdigest(),
        })

    with open(os.path.join(out_dir, "index", "enums.json"), "w", encoding="utf-8") as f:
        json.dump({"dynasty": S.DYNASTY, "kind": S.KIND,
                   "dynasty_label": S.DYNASTY_LABEL,
                   "id_layout": {"dynasty_bits": 4, "kind_bits": 3, "seq_bits": 24}},
                  f, ensure_ascii=False)

    # 清理陈旧分片（上次构建留下、本次未生成的）
    keep = {os.path.basename(s["file"]) for s in manifest["shards"]}
    keep |= {os.path.basename(i["file"]) for i in manifest.get("index", [])}
    stale = [f for f in os.listdir(os.path.join(out_dir, "shards"))
             if f.endswith(".jsonl.zst") and f not in keep]
    if stale:
        if len(stale) <= 20:
            for f in stale:
                os.remove(os.path.join(out_dir, "shards", f))
            print("  清理陈旧分片 %d 个" % len(stale))
        else:
            warn("检测到 %d 个陈旧分片未清理（超过安全阈值 20），请手动删除 %s/shards 后重跑"
                 % (len(stale), out_dir))

    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    total = sum(s["bytes"] for s in manifest["shards"]) + sum(i["bytes"] for i in manifest.get("index", []))
    return total, manifest


# ============================================================ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "dist"))
    ap.add_argument("--db-only", action="store_true")
    ap.add_argument("--bundle-only", action="store_true")
    ap.add_argument("--with-optional", action="store_true", help="额外打包御定全唐詩")
    ap.add_argument("--with-strains", action="store_true",
                    help="额外出平仄可选包（poetry-strains.db + bundle-strains/）")
    ap.add_argument("--strains-only", action="store_true", help="只出平仄可选包")
    ap.add_argument("--no-external", action="store_true",
                    help="忽略 external/ 目录下的外接数据（歌赋/清代诗词/毛选…）")
    ap.add_argument("--no-extras", action="store_true",
                    help="不把 extras/extras-*.jsonl 分片落成 poem_extras 表"
                         "（主库回到 ~150 MB）。注意：注释也只写在 JSONL 里了，"
                         "这种库 poems 没有 notes 列、poem_extras 也是空的")
    ap.add_argument("--with-fts", action="store_true",
                    help="建 FTS5 trigram（实测体积 x4.2 且不支持 <3 字，默认关）")
    ap.add_argument("--zstd-level", type=int, default=12)
    ap.add_argument("--limit", type=int, default=0, help="每个集合只取前 N 条（冒烟用）")
    args = ap.parse_args()

    want_strains = args.with_strains or args.strains_only
    os.makedirs(args.out, exist_ok=True)
    t_all = time.time()

    colls = [c for c in SRC.COLLECTIONS if (not c.optional) or args.with_optional]
    n_ext = 0
    if not args.no_external:
        try:
            ext = SRC.discover_external()
        except (ValueError, json.JSONDecodeError) as e:
            sys.exit("外接数据配置错误：%s" % e)
        if ext:
            colls = colls + ext
            n_ext = len(ext)
            print("外接集合 %d 个：%s" % (n_ext, "、".join(c.label or c.name for c in ext)))
    print("=== 1. 读取与规范化 (%d 个集合) ===" % len(colls))
    poems = build_poems(colls, args.limit)
    print("  合计 %d 条" % len(poems))

    print("\n=== 2. 关联数据挂接 ===")
    # 平仄只在明确要出包时才读 strains/（104 MB JSON，不读能省十几秒）。
    # 不读 = 库里没有平仄，这是配置选择不是数据丢失，所以不告警。
    if want_strains:
        attach_strains(poems, colls)
    else:
        print("  平仄：未启用（--with-strains 才出包），跳过 strains/ 读取")
    # 热度不在这里挂：它按 uid 关联（heat-scores.json），必须等第 3 步归一+去重之后

    print("\n=== 3. 归一（简体 / 去空格 / 佚名置空）· 去重 · 热度挂接 · 标签增强 ===")
    normalize_all(poems)
    poems = dedup(poems)
    # 热度必须在归一+去重之后挂：make_uid 用归一后的字段，去重会丢重复行
    attach_heat(poems, colls)
    enrich_tags(poems)

    print("\n=== 4. 作者库 ===")
    authors = SRC.load_authors()
    seen = set()
    uniq = []
    for a in authors:
        k = (a["name"], a["dynasty"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(a)
    print("  作者 %d 条（去重后 %d）" % (len(authors), len(uniq)))
    uniq = merge_poem_authors(uniq, poems)

    if not args.bundle_only and not args.strains_only:
        print("\n=== 5. 产出 SQLite ===")
        db_path = os.path.join(args.out, "poetry.db")
        snap = snapshot_user_data(db_path)          # 重建前保全用户数据
        size, notes_map = emit_sqlite(poems, uniq, db_path, args.with_fts, want_strains)
        print("  -> %s  %.1f MB" % (db_path, size / 1048576))
        if snap:
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            n, snap_notes = restore_user_data(c, snap)
            c.execute("ANALYZE")
            c.commit()
            c.close()
            # 用户自建/修订条目上的注释并进同一批，由 5.5 步一起落 extras。
            # 不并进来的话，重建会静默吃掉用户自己写的注释（poems 已无 notes 列）。
            notes_map.update(snap_notes)
            print("  -> 已保全并恢复用户数据 %d 条（自建/修订，其中带注释 %d 条），"
                  "重跑 build 不丢" % (n, len(snap_notes)))

        # ---- 5.5 extras 落库：extras.jsonl -> poem_extras，并承接源语料的注释
        # why 必须放在用户数据恢复之后：恢复进来的条目同样可能带注释（用户在 GUI 里
        # 写的），要跟构建期语料的注释合到同一批里一起落库，否则那批注释就丢了。
        print("\n=== 5.5 extras 落库（poem_extras 表） ===")
        c = sqlite3.connect(db_path)
        try:
            # extras 权威源在 poetry-pipeline/extras/（不在 dist/ 下：dist 被 .gitignore
            # 整体忽略，而 extras 是采集成果、重建不出来）。路径统一走 default_root()，
            # 不要再拼 args.out，否则换 --out 目录就找不到 JSONL。
            root_ex = XDB.default_root()
            if args.no_extras:
                # --no-extras：不把 extras.jsonl 重放进主库（那 9.7 万行大文本会把
                # 主库从 144 MB 撑到 ~350 MB）。但注释照样要**写回权威源 JSONL**
                # —— poems 已经没有 notes 列了，不写回就等于把这批注释删掉。
                # 代价：这种库里 poem_extras 表是空的，读注释要直接读 JSONL。
                r = XDB.export_notes(root_ex, notes_map=notes_map)
                print("  --no-extras：跳过 JSONL 重放；注释写回 JSONL %d 条"
                      "（并入已有 %d、跳过 %d）"
                      % (r["written"], r["merged_rows"], r["skipped"]))
                print("  注意：本库 poem_extras 为空，要读注释请走 extras.jsonl")
            else:
                r = XDB.sync(c, root_ex, notes_map=notes_map)
                print("  重放 %d 行（JSONL %d 条）；注释写回 JSONL %d 条"
                      "（并入已有 %d、跳过 %d）"
                      % (r["rows"], r["from_jsonl"], r["notes_written"],
                         r["notes_merged"], r["notes_skipped"]))
                # 这里**不**做 VACUUM：emit_sqlite 刚压过一遍，紧接着的一次性插入
                # 不会留下多少碎片（实测只省 0.7 MB，却要多花 25 秒）。
                # 反复跑 extras_db.py sync 才需要压实，那里有 --vacuum 开关。
                if r["orphans"]:
                    print("  提示：%d 行 poem_extras 在 poems.uid 里找不到对应诗"
                          "（外部数据先到 / 诗已不在库里，属正常，不影响关联检索）"
                          % r["orphans"])
            c.execute("ANALYZE")
            c.commit()
        except XDB.Rejected as e:
            # extras 目录是纯累积数据：新拉一个只有源码的仓库时它不存在，
            # 这不该让整次构建失败——主库其余部分照样可用，只是没有扩展资料。
            warn("extras 未落库（主库其余部分不受影响）：%s" % e)
        finally:
            c.close()
        print("  -> %s  %.1f MB" % (db_path, os.path.getsize(db_path) / 1048576))

    if not args.db_only and not args.strains_only:
        print("\n=== 6. 产出 bundle ===")
        bdir = os.path.join(args.out, "bundle")
        total, manifest = emit_bundle(poems, uniq, bdir, args.zstd_level,
                                      with_strains=False)
        print("  -> %s  %.1f MB (%d 分片)" % (bdir, total / 1048576, manifest["total_shards"]))

    if want_strains:
        print("\n=== 7. 产出平仄可选包 ===")
        emit_strains(poems, args.out, args.zstd_level,
                     db=not args.bundle_only, bundle=not args.db_only)

    print("\n=== 8. 汇总 ===")
    src_bytes = 0
    # 「源库」= SRC.ROOT 下除本工程自己以外的数据。本工程目录必须排除：extras/_cache/
    # 下的 JSON 是上游抓取缓存（目前已 27.9 MB），把它们算进"源库 JSON 总量"会让这个数字
    # 随抓取进度变化，而它本来是用来说明"重构前有多少源数据"的。
    own_dir = os.path.basename(os.path.dirname(os.path.abspath(__file__)))
    skip = {".venv", ".git", "__pycache__", ".pytest_cache", ".idea", ".github", "build", own_dir}
    for dirpath, dirnames, filenames in os.walk(SRC.ROOT):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            if fn.endswith(".json"):
                src_bytes += os.path.getsize(os.path.join(dirpath, fn))
    print("  源库 JSON 总量 = %.1f MB" % (src_bytes / 1048576))
    for name in ("poetry.db", "bundle", "poetry-strains.db", "bundle-strains"):
        p = os.path.join(args.out, name)
        if os.path.isfile(p):
            print("  %-12s = %.1f MB" % (name, os.path.getsize(p) / 1048576))
        elif os.path.isdir(p):
            s = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(p) for f in fs)
            print("  %-12s = %.1f MB" % (name, s / 1048576))
    if LOG_WARN:
        print("\n  警告 %d 条：" % len(LOG_WARN))
        for w in LOG_WARN[:20]:
            print("    - " + w)
    print("\n总耗时 %.1fs" % (time.time() - t_all))


if __name__ == "__main__":
    main()
