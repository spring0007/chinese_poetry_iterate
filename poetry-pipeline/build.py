# -*- coding: utf-8 -*-
"""
构建管线：chinese-poetry 源库 -> 统一格式产物。

产物分三块，共用同一套规范化中间层：
  1. poetry.db            SQLite 主库          —— 服务端 / 桌面 / iOS / Android / Flutter
  2. bundle/              JSONL 分片 + zstd     —— Web / 小程序 / 离线包 / CDN 按需拉取
  3. poetry-strains.db   平仄可选包（--with-strains 才出）
     + bundle-strains/   平仄分片

平仄为什么独立：它只覆盖唐诗/宋诗（311,786 条 = 全库 90.3%），宋词/元曲/蒙学一律没有。
主包带上它要为所有用不到平仄的场景多传一份字节，也让主库无法按"要不要平仄"分别分发。

用法:
    python build.py --out ./dist                  # 主库 + bundle
    python build.py --out ./dist --db-only
    python build.py --out ./dist --bundle-only
    python build.py --out ./dist --with-strains   # 额外出平仄包（db + 分片）
    python build.py --out ./dist --strains-only   # 只出平仄包
    python build.py --out ./dist --with-optional  # 额外打包御定全唐詩（异文版本）
    python build.py --out ./dist --no-external    # 忽略 external/ 下的外接数据
    python build.py --out ./dist --with-fts       # 建 FTS5（体积 x4.2，默认关）
    python build.py --out ./dist --limit 3000     # 冒烟：每个集合只取前 N 条

依赖: Python 3.11+ / zstandard==0.23.0 / zhconv==1.4.3
"""

from __future__ import annotations
import os, sys, json, time, sqlite3, hashlib, shutil, argparse, collections
from typing import Dict, List, Tuple, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema as S
from schema import Poem, make_id, make_uid, parse_id, pack_strains, quantize_score
import sources as SRC

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

            poems.append(Poem(
                id=make_id(dynasty, kind, seq),
                dynasty=dynasty, kind=kind,
                title=SRC.pick_title(rec, coll, lines),
                author=author,
                lines=lines,
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
    """strains/ 按 UUID 关联。未命中的保持 None（不是错误：宋词/元曲本就没有平仄）。"""
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
def _rank_key(rec: dict, kind: str) -> tuple:
    """rank/poet 用 (author,title)；rank/ci 只有 (author,rhythmic)。"""
    if kind == "ci":
        return (rec.get("author", ""), rec.get("rhythmic", ""))
    return (rec.get("author", ""), rec.get("title", ""))


def attach_rank(poems: List[Poem], colls: List[SRC.Collection]):
    """
    rank/ 没有 id。曾经试过按分片序号位置对齐，实测只能命中 3.5%——
    rank 是按旧快照抓的，与当前库顺序早就对不上。
    因此只用内容键连接：(author,title) 对诗，(author,rhythmic) 对词，
    覆盖率 99%+。键冲突（同一作者同名词作）取最大值，并在覆盖率不足时告警。
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


def merge_poem_authors(authors: List[dict], poems: List[Poem]) -> List[dict]:
    """
    作者 id 字典不能只由作者库文件（authors.tang/song/ci/nantang）构成。

    这几个文件只覆盖唐/宋/五代，汉（曹操）、元（元曲作者）、清（张潮）以及
    楚辞、蒙学里出现的姓名根本不在里面。只按作者库建 id 的话，
    author_ids.get(name) 取不到 → 落 0 → 与"佚名"同义，等于把真作者静默抹掉。
    这是最难发现的一类脏数据：表面上一切正常，只有按作者检索时人才不见了。
    """
    known = {a["name"] for a in authors}
    miss: Dict[str, str] = {}
    for p in poems:
        if p.author and p.author not in known and p.author not in miss:
            miss[p.author] = p.dynasty
    for nm in sorted(miss):
        authors.append({"name": nm, "dynasty": miss[nm], "desc": "", "src": "poem-records"})
    if miss:
        print("  补齐正文出现、作者库缺失的姓名 %d 个，例：%s"
              % (len(miss), "、".join(sorted(miss)[:8])))
    return authors


# ============================================================ 4. 去重 + 标签增强
def normalize_all(poems: List[Poem]):
    """
    全库文本归一：转简体 + 去空格 + 佚名作者置空。
    why 全部转简体而不是保留原文：源库里唐诗/宋诗是繁体、宋词/元曲是简体，
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
    overlays = [("全唐诗/唐诗三百首.json", "tang"), ("宋词/宋词三百首.json", "song")]
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
    con.execute("CREATE TABLE authors("
                "id INTEGER PRIMARY KEY, name TEXT NOT NULL, dynasty TEXT NOT NULL DEFAULT '',"
                "desc TEXT NOT NULL DEFAULT '', n_poems INTEGER NOT NULL DEFAULT 0)")
    con.execute("CREATE TABLE rhythmics(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)")
    con.execute("CREATE TABLE sources(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)")

    # 主表不存 dynasty/kind：id 高位已编码（见 schema.make_id），用 parse_id 反解。
    # title 直接存字符串，不进字典表——实测 273,134 个唯一标题 / 345,354 首诗，
    # 重复率只有 1.3 次/值，字典表省不下数据，却要多付一份表 + 唯一索引 + 普通索引
    # 共 29.3 MB；逐行存只要 2.71 MB。字典化只对高重复列（作者 25.7 次/值、
    # 词牌 236 次/值、来源 23024 次/值）才划算。
    # 佚名用 author_id=0 表示（0 不指向任何 authors 行，即 NULL 语义）。
    con.execute("""
        CREATE TABLE poems(
            id INTEGER PRIMARY KEY,
            uid TEXT NOT NULL,
            author_id INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL DEFAULT '',
            rhythmic_id INTEGER NOT NULL DEFAULT 0,
            src_id INTEGER NOT NULL DEFAULT 0,
            body TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            score INTEGER NOT NULL DEFAULT 0,
            n_char INTEGER NOT NULL DEFAULT 0
        )""")
    # 平仄不在这里建表：它是可选包（见 emit_strains）。主库保持"只有检索必需的东西"。

    author_ids = {a["name"]: i + 1 for i, a in enumerate(authors)}
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
    for p in poems:
        rows.append((
            p.id,
            make_uid(p.dynasty, p.author, p.title, p.body),
            author_ids.get(p.author, 0),
            p.title,
            _tid(rhythmic_ids, p.rhythmic),
            _tid(src_ids, p.src),
            p.body,
            "\x1f".join(p.tags),
            "\x1f".join(p.notes),
            p.score,
            p.n_char,
        ))

    lost = sum(1 for p in poems if p.author and author_ids.get(p.author, 0) == 0)
    if lost:
        warn("%d 条记录有作者名却拿不到 author_id，会被误当成佚名" % lost)

    con.executemany(
        "INSERT INTO poems(id,uid,author_id,title,rhythmic_id,src_id,body,tags,notes,"
        "score,n_char) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)

    n_by_author = collections.Counter(p.author for p in poems if p.author)
    con.executemany("INSERT INTO authors VALUES(?,?,?,?,?)", [
        (i + 1, a["name"], a["dynasty"], a["desc"], n_by_author.get(a["name"], 0))
        for i, a in enumerate(authors)])
    con.executemany("INSERT INTO rhythmics VALUES(?,?)", [(v, k) for k, v in rhythmic_ids.items()])
    con.executemany("INSERT INTO sources VALUES(?,?)", [(v, k) for k, v in src_ids.items()])
    print("  poems %d 行 / authors %d / rhythmics %d"
          % (len(rows), len(authors), len(rhythmic_ids)))

    # 注意：rhythmics.name / sources.name 已声明 UNIQUE，SQLite 会自动建
    # sqlite_autoindex_*，再手动建一个同名索引就是纯浪费（实测白白多占一份）。
    # uid 是给外部数据（extras 等）关联用的主键，必须唯一；顺带承担点查。
    # why 不直接拿它当 PRIMARY KEY：id 的 int32 高位编码 dynasty/kind 是查询
    # 与分片的基础（见 schema.make_id），uid 只是「对外稳定引用」，两者分工不同。
    con.execute("CREATE UNIQUE INDEX ix_poems_uid ON poems(uid)")
    con.execute("CREATE INDEX ix_poems_author ON poems(author_id)")
    con.execute("CREATE INDEX ix_poems_title ON poems(title)")
    con.execute("CREATE INDEX ix_poems_rhythmic ON poems(rhythmic_id)")
    con.execute("CREATE INDEX ix_poems_score ON poems(score DESC)")
    con.execute("CREATE INDEX ix_authors_name ON authors(name)")

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
        ("text_form", "simplified-nospace"),
        ("fts", "1" if with_fts else "0"),
        ("strains_pkg", "1" if with_strains else "0"),
        ("note", "全库简体归一、无空格。body 兼作检索列，查询词需先转简体。"
                 "dynasty/kind 由 id 高位反解；author_id=0 表示佚名。"
                 "平仄在独立可选包 poetry-strains.db，通过 id 关联。"
                 "uid 是内容派生的稳定 uuid：外部数据（extras）一律用 uid 关联，"
                 "不要用 id —— id 含序号，会因 dedup/重排而整体位移。"),
    ])
    # ANALYZE 必须有：没有 sqlite_stat1 时，SQLite 会为了吃掉 ORDER BY score DESC
    # 而选 ix_poems_score 做全索引扫描，把 p.title = ? 这种高选择性查询拖到 900ms+。
    # 有统计信息后它会正确选择 ix_poems_title。代价只有几 KB。
    con.execute("ANALYZE")
    con.commit()
    con.execute("VACUUM")
    con.close()
    return os.path.getsize(out_db)


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
    rows = con.execute("""
        SELECT p.id, p.author_id, p.title, p.rhythmic_id, p.src_id, p.body, p.tags,
               p.notes, p.score, p.n_char, s.name AS src_name, a.name AS author_name,
               a.dynasty AS author_dyn, r.name AS rhythmic_name
        FROM poems p
        JOIN sources s ON s.id = p.src_id
        LEFT JOIN authors a ON a.id = p.author_id
        LEFT JOIN rhythmics r ON r.id = p.rhythmic_id
        WHERE s.name IN ('user-added', 'user-edit')
    """).fetchall()
    out = [dict(x) for x in rows]
    con.close()
    return out


def restore_user_data(con, snapshot) -> int:
    """
    把快照重放到新建的 con 上：保留原 id（修订 = 覆盖同 id 的构建期行；
    自建 = custom 号段不冲突）。作者 / 词牌按名复用新库字典，找不到就新建。
    最后用 GROUP BY 重算 authors.n_poems，保证计数一致。
    """
    if not snapshot:
        return 0
    for src in ("user-added", "user-edit"):
        con.execute("INSERT OR IGNORE INTO sources(name) VALUES(?)", (src,))
    authors = {r[0]: r[1] for r in con.execute("SELECT name, id FROM authors")}
    rhythmics = {r[0]: r[1] for r in con.execute("SELECT name, id FROM rhythmics")}
    src_ids = {r[0]: r[1] for r in con.execute("SELECT name, id FROM sources")}
    for p in snapshot:
        aname = p["author_name"]
        aid = 0
        if aname:
            aid = authors.get(aname)
            if aid is None:
                cur = con.execute(
                    "INSERT INTO authors(name,dynasty,desc,n_poems) VALUES(?,?,?,0)",
                    (aname, p["author_dyn"] or "", ""))
                aid = cur.lastrowid
                authors[aname] = aid
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
        con.execute(
            "INSERT OR REPLACE INTO poems(id,uid,author_id,title,rhythmic_id,src_id,body,tags,notes,score,n_char) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (p["id"], make_uid(_dyn, aname or "", p["title"], p["body"]),
             aid, p["title"], rid, sid, p["body"], p["tags"], p["notes"],
             p["score"], p["n_char"]))
    con.execute("UPDATE authors SET n_poems=0")
    counts = con.execute(
        "SELECT author_id, COUNT(*) c FROM poems WHERE author_id!=0 GROUP BY author_id").fetchall()
    con.executemany("UPDATE authors SET n_poems=? WHERE id=?", [(c, a) for a, c in counts])
    con.commit()
    return len(snapshot)


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
    attach_rank(poems, colls)

    print("\n=== 3. 归一（简体 / 去空格 / 佚名置空）· 去重 · 标签增强 ===")
    normalize_all(poems)
    poems = dedup(poems)
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
        size = emit_sqlite(poems, uniq, db_path, args.with_fts, want_strains)
        print("  -> %s  %.1f MB" % (db_path, size / 1048576))
        if snap:
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            n = restore_user_data(c, snap)
            c.execute("ANALYZE")
            c.commit()
            c.close()
            print("  -> 已保全并恢复用户数据 %d 条（自建/修订），重跑 build 不丢" % n)

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
    for dirpath, dirnames, filenames in os.walk(SRC.ROOT):
        dirnames[:] = [d for d in dirnames if d not in {".venv", ".git", "__pycache__", ".pytest_cache", ".idea", ".github", "build"}]
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
