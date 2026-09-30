# -*- coding: utf-8 -*-
"""
桌面窗口：检索 + 编辑 + 添加。

why Tkinter 而不是 Electron/Qt：零第三方依赖（标准库自带），Windows/macOS/Linux
都能跑，单文件双击即开。代价是界面朴素——对一个"查数据、录数据"的工具来说，
可读性 > 视觉效果，且省掉整条前端构建链。

三个能力（标签页 + 按钮）：
  检索：关键词 / 朝代 / 体裁 / 作者 / 词牌 过滤，结果表 + 详情（含平仄）；
        选中一条可「编辑选中」或「删除选中」——用于订正显示有误的诗词。
  添加：录入新条目。文本走与构建期完全相同的归一规则，写完立刻能被搜到。
  编辑：弹出预填窗口改 标题/作者/词牌/标签/注释/正文，保存即写回数据库；
        朝代、体裁不可改（会变 id，破坏平仄关联）。修订过的构建期数据会被
        标记为"用户修订"来源，与原始构建数据区分。

运行：
    python app.py
    python app.py --db ./dist/poetry.db --strains-db ./dist/poetry-strains.db
"""

from __future__ import annotations
import os, sys, time, threading, argparse, queue

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

import schema as S
import query as Q
import store as ST
import extras as X
import sources as SRC          # 只为「反查 id 出处 / 写回源 JSON」，与 build.py 共用一套读法
import patchjson               # 语料 JSON 的字节级最小改写（与 reconcile_gui 共用一份）

FONT = ("Microsoft YaHei UI", 10)
FONT_SM = ("Microsoft YaHei UI", 9)
MONO = ("Consolas", 10)

# 扩展资料在详情面板里的展示顺序与中文名。
# 顺序按"读者实际查阅动线"排：先懂字面（译文/注释），再懂背景，最后看赏析。
XTRA_LABELS = (
    ("translation", "译文"),
    ("notes", "注释"),
    ("introduction", "作品简介"),
    ("background", "创作背景"),
    ("appreciation", "赏析"),
)

HERE = os.path.dirname(os.path.abspath(__file__))


def _default(rel: str) -> str:
    return os.path.join(HERE, rel)


# ============================================================ 编辑写回源语料 JSON
#
# why 要写回：dist/poetry.db 是 build.py 从语料 JSON 派生出来的派生物。只在库里改，
# 下次重建就被覆盖回去。所以「改一条已有记录」必须落回它出处的那个 JSON 分片；
# 「新增」和「删除」不动语料——自建条目本来就没有出处。
#
# 定位办法：poems.id 的高位编码 (朝代, 体裁)，低位是 build_poems 里那个按
# (朝代,体裁) 分桶的全局自增序号。把该桶的赋值顺序原样重放一遍，就能把 id
# 反解成 (集合, 文件, 数组下标)，既不用给数据库加溯源列，也不用重建。
# 重放只读目标号段涉及的分片，通常零点几秒。
#
# 写回统一走 patchjson.patch_file：只替换目标记录的指定字段字面量，写盘前复核
# 「其他记录逐字节未变、目标字段确已变成新值」，复核不过就拒绝写盘。
# 换言之：定位可能不准，但改坏语料这件事被复核挡在门外。

WRITEBACK_BACKUP = os.path.join(HERE, "xcheck", "patch-backup")


def _short_path(path: str) -> str:
    """把绝对路径缩成相对工程根／语料根，提示框里好读。"""
    for base in (HERE, SRC.ROOT):
        try:
            rel = os.path.relpath(path, base)
        except ValueError:                      # 跨盘符
            continue
        if not rel.startswith(".."):
            return rel.replace("\\", "/")
    return path


def _shard_stream(coll):
    """复刻 sources.iter_collection，额外给出「数组下标」和「记录数组藏在哪个键下」。

    下标与产出序号是两回事：iter_collection 会静默跳过非 dict 元素，而
    patchjson 要按原始数组下标定位记录（非 dict 元素照样占位）。两者在出现
    非 dict 时就会分叉，必须分开数。
    """
    if coll.external:
        spec = SRC.load_external_spec(coll.pattern)
        for i, rec in enumerate(spec.get("items") or []):
            if not isinstance(rec, dict):       # iter_external 在这里是 raise，不是跳过
                raise ValueError("%s：items[%d] 不是对象" % (coll.pattern, i))
            yield coll.pattern, i, rec, ("items",)
        return
    for path in SRC.glob_shards(coll.pattern):
        data = SRC.load_json(path)
        if coll.wrap_single or isinstance(data, dict):
            data = [data]
        for i, rec in enumerate(data):
            if isinstance(rec, dict):
                yield path, i, rec, ()


class SourceLocator:
    """poem.id -> (集合, 文件, 数组下标, 记录数组所在的键) 的反查表。

    按 (朝代, 体裁) 分桶、用到哪桶才读哪桶（懒加载 + 进程内缓存）。
    集合清单以数据库 sources 表为准而不是 sources.py 的 COLLECTIONS——
    这样 --no-external / --with-optional / 外接集合 enabled=false 三种情况
    都自动跟随「这次构建实际用了哪些集合」；反过来，如果库里的来源现在找不到
    对应集合（语料目录被改过、或库是别的数据根构建的），就整体拒绝写回。
    """

    def __init__(self, con):
        self.con = con
        self._buckets = {}
        self.err = ""

    @property
    def warm(self) -> bool:
        """是否已经建过索引（没有＝第一次保存，多半要等一两秒）。"""
        return bool(self._buckets)

    def _colls(self):
        names = set(r[0] for r in self.con.execute("SELECT name FROM sources"))
        out = [c for c in SRC.COLLECTIONS if c.name in names]
        out += [c for c in SRC.discover_external() if c.name in names]
        missing = names - set(c.name for c in out) - {ST.USER_SRC, ST.EDIT_SRC}
        if missing:
            self.err = ("数据库里的来源 %s 找不到对应集合，无法确定语料出处"
                        "（语料目录被改过？或这份库是别的数据根构建的？）"
                        % "、".join(sorted(missing)))
        return out

    def _index(self, key):
        if key in self._buckets:
            return self._buckets[key]
        m = {}
        seq = 0
        for coll in self._colls():
            if not coll.external and (coll.dynasty, coll.kind) != key:
                continue                    # 别的号段不动这桶的计数，整片文件都不用读
            for path, idx, rec, array_path in _shard_stream(coll):
                lines = SRC.get_body(rec, coll)
                if not lines or not any(l.strip() for l in lines):
                    continue                # 空正文不计入序号（与 build_poems 一致）
                dyn, kind = coll.dynasty, coll.kind
                if coll.external:
                    dyn = rec.get("dynasty") or dyn
                    kind = rec.get("kind") or kind
                if (dyn, kind) != key:
                    continue
                m[S.make_id(dyn, kind, seq)] = (coll, path, idx, array_path)
                seq += 1
        self._buckets[key] = m
        return m

    def locate(self, pid):
        if self.err:
            return None
        dyn, kind, _seq = S.parse_id(pid)
        return self._index((dyn, kind)).get(pid)


def _read_record(loc):
    """写回前现读一遍文件（不用任何缓存），把定位到的那条记录交出来。"""
    coll, path, idx, array_path = loc
    if array_path:
        rec = (SRC.load_external_spec(path).get("items") or [])[idx]
        return rec
    data = SRC.load_json(path)
    if coll.wrap_single or isinstance(data, dict):
        data = [data]
    return data[idx]


def _body_field(rec, coll):
    """get_body 实际读到的那一个键，以及它能不能安全回写。

    返回 (键, 可回写, 原值是不是单个字符串)。可回写为 False 的两种情况：
    没有该字段；或该字段是嵌套结构——蒙学的 content 是 [{title, content:[…]}]、
    四书五经是章节对象，get_body 靠 _flatten 硬展平成行，写回去会摧毁章节结构。
    这是「宁可少同步，也不改坏语料」里最典型的一处，只能拒绝。

    键按 get_body 的取值顺序找：coll.body → paragraphs → content，
    这样写回的就是当初读进来的那一个字段，不会另起一个平行字段。
    """
    keys = [coll.body]
    if coll.body != "paragraphs":
        keys.append("paragraphs")
    keys.append("content")
    for k in keys:
        if not k:
            continue
        v = rec.get(k)
        if v is None:
            continue
        if isinstance(v, str):
            return k, True, True
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            return k, True, False
        return k, False, False
    return "", False, False


def _project(rec, coll, lines):
    """按 build.py 的口径把源记录投影成库里的三个文本字段，用于同一性自检。

    必须重建而不是取 rec.get("title")：宋词的标题来自词牌、幽梦影/曹操的作者是
    coll.default_author 补录的、正文有四个可能的键——照搬 build 的表达式才比得准。
    """
    author = (rec.get(coll.author) or "").strip() if coll.author else ""
    if not author:
        author = (rec.get("author") or "").strip()
    if not author:
        author = coll.default_author
    return (S.norm_text(SRC.pick_title(rec, coll, lines)),
            S.norm_author(author),
            S.norm_lines(lines))


def _merge_lines(src_raw, src_norm, new_norm):
    """行级三方合并：只有真正改过的行才换成新值，其余行逐字节保留语料原文。

    不这么做的话，改一个错字会把整首诗的繁体字形（与标题里的空格）一并抹成
    库里的简体归一值——那些空格在归一后是不可逆的。
    """
    import difflib
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            a=src_norm, b=new_norm, autojunk=False).get_opcodes():
        out.extend(src_raw[i1:i2] if tag == "equal" else new_norm[j1:j2])
    return out


def row_view(row):
    """把 Q.get_by_id 的一行摊平成写回用得上的取值（正文/注释按行、按项拆开）。

    库里存的就是 update_poem 归一后的值，所以这里不需要再过一遍 norm_*。
    """
    return {
        "title": row["title"] or "",
        "author": row["author"] or "",
        "lines": (row["body"] or "").split("\n") if row["body"] else [],
        "rhythmic": row["rhythmic"] or "",
        "notes": (row["notes"] or "").split("\x1f") if row["notes"] else [],
    }


def writeback_record(con, pid, old_row, new_row, loc, dry=False) -> dict:
    """把一次编辑里「真正改过的字段」写回该记录出处的 JSON 分片。

    old_row / new_row：改前、改后的 Q.get_by_id 行（库里存的就是归一后的值，
    所以新旧直接比即可）。loc：SourceLocator.locate 的结果。

    返回 {"level","msg"}，level ∈ written / noop / refused / skip，供界面分档提示。
    """
    coll, path, idx, array_path = loc
    base = os.path.basename(path)
    rec = _read_record(loc)
    old, new = row_view(old_row), row_view(new_row)

    lines = SRC.get_body(rec, coll)
    bkey, b_flat, b_as_str = _body_field(rec, coll)
    proj_t, proj_a, _pl = _project(rec, coll, lines)

    # 自检：把源记录按 build 口径重建成 (标题, 作者, 正文)，与改前的库值逐项比。
    # 对上了才说明「定位到的这条」确实是库里那条；对不上就宁可不动语料。
    # 正文这一项用的是 get_body 的输出——嵌套结构也能比，比的是展平后的结果。
    src_norm = S.norm_lines(lines)
    bad = [n for n, ok in (("标题", proj_t == old["title"]),
                           ("作者", proj_a == old["author"]),
                           ("正文", src_norm == old["lines"])) if not ok]
    # 三项里错两项以上 → 拿不准是不是同一条，一律不动语料。
    # 另外，正文不可回写（嵌套结构）时正文这一项比对不上也要拒绝：蒙学/四书五经这类
    # 集合的「标题+作者」根本不是身份标识（四书五经的标题就是章节名、作者恒为空），
    # 一旦语料被加删过记录、下标整体挪位，只有正文能识别出「定位错了」。
    # 重建一次库就能让源与库重新对齐，之后照常写回。
    if len(bad) > 1 or (bad and not b_flat):
        return {"level": "refused",
                "msg": ("定位到的源记录与数据库对不上（%s），可能是语料已被改过、"
                        "这份库已过期、或这条记录在构建期被去重丢弃，未写回。\n"
                        "源 %s：%s／%s／%s\n库：%s／%s／%s\n"
                        "（重建一次库即可让源语料与库重新对齐）"
                        % ("、".join(bad), base, proj_t, proj_a, "／".join(src_norm[:1]),
                           old["title"], old["author"], "／".join(old["lines"][:1])))}
    note = ("；%s与库不一致（可能上次写回改过），未动" % "、".join(bad)) if bad else ""
    # 正文只在「源字段是扁平字符串数组 且 正文这一项校验通过」时才敢按行合并：
    # 校验没过，行就对不齐，合并出来的是另一首诗的字。
    body_ok = b_flat and not bad
    src_raw = [l for l in lines if S.norm_text(l)] if body_ok else []

    edited = {"title": new["title"] != old["title"],
              "author": new["author"] != old["author"],
              "body": new["lines"] != old["lines"],
              "rhythmic": new["rhythmic"] != old["rhythmic"],
              "notes": new["notes"] != old["notes"]}
    if not any(edited.values()):
        return {"level": "skip", "msg": "标题/作者/正文/词牌/注释都没变，未动源文件"}
    changed = dict(edited, body=edited["body"] and body_ok)

    # 语料里已经是用户想要的值（库是旧的）：不用写，重建一次就同步了。
    # 三条限制缺一不可：①三项必须都真的比过——正文比不了（嵌套结构）时不能拿
    # 「没比」当「相同」，否则改正文会被报成「源语料里已经是这次的取值了」；
    # ②本判断只覆盖标题/作者/正文，所以词牌或注释改过时必须继续往下走，
    # 否则「只改词牌」会被误判成无需写回（宋词的词牌恰恰是最常改的字段之一）。
    if (not edited["rhythmic"] and not edited["notes"]
            and proj_t == new["title"] and proj_a == new["author"]
            and src_norm == new["lines"]):
        return {"level": "noop",
                "msg": "源语料里已经是这次的取值了（这份库是旧的），重建一次即可同步"}

    edits, done, skipped = [], [], []
    if changed["title"]:
        if coll.title:
            edits.append((idx, coll.title, rec.get(coll.title), new["title"]))
            done.append("标题")
        else:
            skipped.append("标题（该集合没有标题字段）")
    if changed["author"]:
        if not new["author"] and coll.default_author:
            skipped.append("作者（该集合的作者是补录的 %s，清空表达不出「佚名」）"
                           % coll.default_author)
        else:
            akey = coll.author or "author"
            edits.append((idx, akey, rec.get(akey), new["author"]))
            done.append("作者")
    if changed["body"]:
        merged = _merge_lines(src_raw, src_norm, new["lines"])
        val = "\n".join(merged) if b_as_str else merged
        if val != rec.get(bkey):
            edits.append((idx, bkey, rec.get(bkey), val))
            done.append("正文")
    elif edited["body"]:
        skipped.append("正文（%s）" % ("源记录与库不一致" if b_flat else "源字段是嵌套结构"))
    if changed["rhythmic"]:
        if coll.rhythmic:
            edits.append((idx, coll.rhythmic, rec.get(coll.rhythmic), new["rhythmic"]))
            done.append("词牌")
        else:
            skipped.append("词牌（该集合没有词牌字段）")
    if changed["notes"]:
        raw_notes = rec.get(coll.notes) if coll.notes else None
        if raw_notes is None:
            skipped.append("注释（该集合没有注释字段）")
        elif isinstance(raw_notes, str) and len(new["notes"]) != 1:
            skipped.append("注释（源字段是单个字符串，装不下 %d 条）" % len(new["notes"]))
        else:
            val = new["notes"][0] if isinstance(raw_notes, str) else new["notes"]
            edits.append((idx, coll.notes, raw_notes, val))
            done.append("注释")
    # 标签不写回：库里的 tags 混着 build 期由「唐诗三百首/宋词三百首」回填的派生标签，
    # 写回去等于把派生标签烙进语料，下次构建又会叠一层。

    if not edits:
        return {"level": "skip",
                "msg": "源文件里没有可写的对应字段：%s" % "、".join(skipped) if skipped
                       else "没有需要写回的改动"}

    res = patchjson.patch_file(path, edits, WRITEBACK_BACKUP, dry=dry,
                               array_path=array_path)
    if not res["ok"]:
        return {"level": "refused", "msg": "源文件未写回：%s" % res["msg"]}
    return {"level": "written" if not dry else "noop",
            "msg": "%s 第 %d 条：%s%s%s" % (base, idx, "、".join(done),
                                            note, ("；未写：" + "、".join(skipped)) if skipped else ""),
            "path": path, "idx": idx, "fields": done, "backup": res["msg"]}



class App:
    def __init__(self, root: tk.Tk, db: str, strains_db: str):
        self.root = root
        self.db = db
        self.strains_db = strains_db
        root.title("诗词库 · 查询 / 编辑 / 添加")
        root.geometry("1080x720")
        root.minsize(900, 600)


        self.con = None
        self.err = ""
        # 检索分页状态：q_args 记查询条件，翻页时按同一条件取不同偏移量
        self.q_args = {}
        self.q_page = 1
        self.q_total = 0
        self.q_size = 50
        # 检索结果回传队列。why：实测（Python 3.14 / Tk 8.6）子线程里调 root.after()
        # 根本不会被执行，回调永远不触发，界面会一直停在"检索中…"。
        # 所以子线程只把结果塞进队列，由主线程用 after() 轮询取回——
        # 所有 tkinter 调用都留在主线程，这也是 Tk 唯一可靠的用法。
        self._q = queue.Queue()
        self._pumping = False
        try:
            self.con = ST.open_rw(db, strains_db)
        except Exception as e:          # 开库失败也要起来，至少能告诉用户原因
            self.err = str(e)

        # 核对状态库（独立 sidecar，重建 poetry.db 不会影响它）。rw=True 时
        # 若文件不存在会自动建表，因此首次标记核对就会生成 poetry-checks.db。
        self.chk_db = _default(os.path.join("dist", "poetry-checks.db"))
        try:
            self.chk_con = ST.open_checks(self.chk_db, rw=True)
        except Exception as e:
            self.chk_con = None
            self.err = self.err or str(e)
        self.cur_verified = False

        # 写回源语料用：id→(集合,文件,下标) 的反查表，第一次保存时才建（懒加载）。
        self._src_loc = None

        # 扩展属性（译文 / 注释 / 作品简介 / 创作背景 / 赏析 / 配图）。
        # 又是一个独立 sidecar（纯文本 JSON + 本地图片目录）：这些内容策展成本高、
        # 且大量依赖外部来源，和"从源码重建出来的原文"生命周期完全不同，
        # 绝不能被 build.py 冲掉。目录不存在时也照样构造——load() 遇到空目录
        # 只会得到空索引，界面正常起来，等用户跑 fetch_extras.py 填充。
        self.extras_root = _default(os.path.join("dist", "extras"))
        self.extras = X.ExtrasStore(self.extras_root).load()

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        self.tab_search = ttk.Frame(nb)
        self.tab_add = ttk.Frame(nb)
        nb.add(self.tab_search, text="  检索  ")
        nb.add(self.tab_add, text="  添加  ")

        self._build_search(self.tab_search)
        self._build_add(self.tab_add)
        self._build_status(root)

        if self.con is None:
            self._set_status("无法打开数据库：%s" % self.err)
        else:
            self._refresh_stats()
            self.refresh_mine()

    # ------------------------------------------------------------ 状态栏
    def _build_status(self, root):
        # side="bottom" 后 pack 的控件贴在最下；先 pack 核对栏，使它成为最底部一行。
        self.chk_status = tk.StringVar(value="")
        chkbar = ttk.Label(root, textvariable=self.chk_status, anchor="w",
                           relief="sunken", font=FONT_SM, foreground="#1a7a2a")
        chkbar.pack(fill="x", side="bottom", ipady=2)
        self.status = tk.StringVar(value="")
        bar = ttk.Label(root, textvariable=self.status, anchor="w",
                        relief="sunken", font=FONT_SM)
        bar.pack(fill="x", side="bottom", ipady=2)

    def _set_status(self, s: str):
        self.status.set(s)

    def _refresh_stats(self):
        if not self.con:
            return
        try:
            st = ST.stats(self.con)
            edited = ST.count_edited(self.con)
            strains = ""
            if self.strains_db and os.path.exists(self.strains_db) and Q.has_strains(self.con):
                n = self.con.execute("SELECT COUNT(*) FROM strainsdb.poem_strains").fetchone()[0]
                strains = "  平仄包 %d 条" % n
            else:
                strains = "  平仄包 未挂载"
            # 扩展资料条数用 len(store)：它读的是内存里的 id->offset 索引，O(1)。
            # why 不调 extras.stats()：那要把整个 JSONL 逐行 JSON 解析一遍，
            # 而这里是每次增删 poem / 切换标签都会走的路径，不能是 O(n)。
            self._set_status(
                "库: %s   共 %d 条 / %d 位作者 / 我的条目 %d 条（自建 %d / 修订 %d）"
                "   扩展资料 %d 条%s"
                % (os.path.basename(self.db), st["n_poems"], st["n_authors"],
                   ST.count_mine(self.con), st["n_user_added"], edited,
                   len(self.extras), strains))
            # 底部核对栏：与上方统计分离，始终显示已核对 / 待核对
            if self.chk_con:
                vc = ST.count_checked(self.chk_con)
                self.chk_status.set("核对状态：已核对 %d 篇 / 待核对 %d 篇（全库共 %d 篇）"
                                    % (vc, st["n_poems"] - vc, st["n_poems"]))
            else:
                self.chk_status.set("核对状态：核对库未打开")
        except Exception as e:
            self._set_status("统计失败：%s" % e)

    # ============================================================ 检索窗口
    def _build_search(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill="x", padx=8, pady=6)

        ttk.Label(top, text="关键词", font=FONT).grid(row=0, column=0, sticky="e", padx=(0, 4))
        self.v_kw = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.v_kw, font=FONT, width=22)
        e.grid(row=0, column=1, sticky="w")
        e.bind("<Return>", lambda _e: self.do_search())

        ttk.Label(top, text="朝代", font=FONT).grid(row=0, column=2, sticky="e", padx=(12, 4))
        self.v_dyn = tk.StringVar(value="（全部）")
        ttk.Combobox(top, textvariable=self.v_dyn, width=8, font=FONT, state="readonly",
                     values=["（全部）"] + ["%s" % S.DYNASTY_LABEL[k] for k in S.DYNASTY]
                     ).grid(row=0, column=3, sticky="w")

        ttk.Label(top, text="体裁", font=FONT).grid(row=0, column=4, sticky="e", padx=(12, 4))
        self.v_kind = tk.StringVar(value="（全部）")
        ttk.Combobox(top, textvariable=self.v_kind, width=7, font=FONT, state="readonly",
                     values=["（全部）"] + ["%s" % S.KIND_LABEL[k] for k in S.KIND]
                     ).grid(row=0, column=5, sticky="w")

        ttk.Label(top, text="作者", font=FONT).grid(row=1, column=0, sticky="e", padx=(0, 4), pady=(6, 0))
        self.v_author = tk.StringVar()
        ttk.Entry(top, textvariable=self.v_author, font=FONT, width=12).grid(
            row=1, column=1, sticky="w", pady=(6, 0))

        ttk.Label(top, text="词牌", font=FONT).grid(row=1, column=2, sticky="e", padx=(12, 4), pady=(6, 0))
        self.v_rhy = tk.StringVar()
        ttk.Entry(top, textvariable=self.v_rhy, font=FONT, width=12).grid(
            row=1, column=3, sticky="w", pady=(6, 0))

        ttk.Label(top, text="每页", font=FONT).grid(row=1, column=4, sticky="e", padx=(12, 4), pady=(6, 0))
        self.v_limit = tk.IntVar(value=50)
        ttk.Spinbox(top, from_=10, to=500, increment=10, textvariable=self.v_limit,
                    width=6, font=FONT).grid(row=1, column=5, sticky="w", pady=(6, 0))

        self.btn_search = ttk.Button(top, text="检索", command=self.do_search)
        self.btn_search.grid(row=0, column=6, rowspan=2, padx=(16, 0))
        ttk.Button(top, text="清空", command=self._clear_search).grid(row=0, column=7, rowspan=2, padx=(6, 0))

        # 结果表
        mid = ttk.Frame(parent)
        mid.pack(fill="both", expand=True, padx=8)
        cols = ("id", "dyn", "kind", "author", "title", "preview", "chk")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=12)
        heads = {"id": ("id", 90), "dyn": ("朝代", 60), "kind": ("体裁", 50),
                 "author": ("作者", 120), "title": ("标题", 180),
                 "preview": ("正文", 360), "chk": ("核对", 44)}
        for c in cols:
            t, w = heads[c]
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w" if c != "id" else "center")
        vs = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.show_detail())
        self._build_context_menu()

        # 分页栏：第 X / Y 页（共 N 条）+ 首页/上/下/末 + 跳页
        pg = ttk.Frame(parent)
        pg.pack(fill="x", padx=8, pady=(2, 4))
        self.btn_first = ttk.Button(pg, text="首页", width=5, command=lambda: self.goto_page(1))
        self.btn_prev = ttk.Button(pg, text="上一页", width=6, command=lambda: self.goto_page(self.q_page - 1))
        self.pg_label = ttk.Label(pg, text="第 1 / 1 页（共 0 条）", font=FONT_SM, width=24, anchor="center")
        self.btn_next = ttk.Button(pg, text="下一页", width=6, command=lambda: self.goto_page(self.q_page + 1))
        self.btn_last = ttk.Button(pg, text="末页", width=5, command=lambda: self.goto_page(10 ** 9))
        self.pg_jump = tk.IntVar(value=1)
        ent_jump = ttk.Entry(pg, textvariable=self.pg_jump, width=5, font=FONT_SM)
        btn_jump = ttk.Button(pg, text="跳转", width=5, command=lambda: self.goto_page(self.pg_jump.get()))
        for w in (self.btn_first, self.btn_prev, self.pg_label, self.btn_next,
                  self.btn_last, ttk.Label(pg, text="跳到", font=FONT_SM),
                  ent_jump, btn_jump):
            w.pack(side="left", padx=2)
        self._render_pager()

        # 详情
        ttk.Label(parent, text="详情", font=FONT).pack(anchor="w", padx=8, pady=(6, 0))
        self.detail = scrolledtext.ScrolledText(parent, height=10, font=MONO, wrap="word")
        self.detail.pack(fill="both", expand=False, padx=8, pady=(0, 8))
        self.detail.configure(state="disabled")

        ebf = ttk.Frame(parent)
        ebf.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(ebf, text="编辑诗词", command=self.edit_selected).pack(side="left")
        ttk.Button(ebf, text="编辑作者", command=self.edit_author_of_selected).pack(side="left", padx=6)
        ttk.Button(ebf, text="扩展资料", command=self.edit_extras).pack(side="left", padx=6)
        ttk.Button(ebf, text="删除选中", command=self.del_selected).pack(side="left", padx=6)
        self.btn_chk = ttk.Button(ebf, text="标记已核对", command=self.toggle_checked)
        self.btn_chk.pack(side="left", padx=6)
        ttk.Label(ebf, text="扩展资料 = 译文/注释/简介/背景/赏析/配图（右键也有菜单）",
                  font=FONT_SM, foreground="#888").pack(side="left", padx=12)

    def _clear_search(self):
        for v in (self.v_kw, self.v_author, self.v_rhy):
            v.set("")
        self.v_dyn.set("（全部）")
        self.v_kind.set("（全部）")
        for i in self.tree.get_children():
            self.tree.delete(i)
        self._detail_text("")

    def _dyn_key(self) -> str:
        lab = self.v_dyn.get()
        for k, v in S.DYNASTY_LABEL.items():
            if v == lab:
                return k
        return ""

    def _kind_key(self) -> str:
        lab = self.v_kind.get()
        for k, v in S.KIND_LABEL.items():
            if v == lab:
                return k
        return ""

    def do_search(self, page: int = 1):
        if not self.con:
            messagebox.showerror("错误", "数据库未打开：%s" % self.err)
            return
        kw = self.v_kw.get().strip()
        author = self.v_author.get().strip()
        rhy = self.v_rhy.get().strip()
        if not kw and not author and not rhy:
            messagebox.showinfo("提示", "请至少填 关键词 / 作者 / 词牌 之一")
            return
        # 记下本次查询条件，翻页时按同一条件重新取偏移量
        self.q_args = dict(kw=kw, dynasty=self._dyn_key(), kind=self._kind_key(),
                           author=author, rhy=rhy)
        self.q_size = max(1, int(self.v_limit.get()))
        self.q_page = page
        self.btn_search.configure(state="disabled", text="检索中…")
        self._set_status("检索中…")
        q, size, off = self.q_args, self.q_size, (page - 1) * self.q_size

        def worker():
            # 子线程：里面不能出现任何 tkinter 调用（实测 Tk 8.6 跨线程 after() 无效）。
            t0 = time.time()
            rows, total, err = [], 0, ""
            try:
                con = ST.open_ro(self.db, self.strains_db)   # 子线程必须用自己的连接
                try:
                    total, rows = Q.run_query(con, **q, limit=size, offset=off)
                finally:
                    con.close()
            except Exception as e:
                err = "%s: %s" % (type(e).__name__, e)
            dt = (time.time() - t0) * 1000
            self._q.put((total, rows, dt, err))    # 不碰 tkinter，只交结果

        threading.Thread(target=worker, daemon=True).start()
        # 主线程起轮询（after 只能在主线程调，且由主线程自己重排）
        if not self._pumping:
            self._pumping = True
            self.root.after(30, self._pump)

    def _pump(self):
        """主线程轮询取回检索结果。没结果就 30 ms 后再来一次。"""
        item = None
        while True:                      # 取到最后一条为止：用户连点检索时，
            try:                         # 中间的旧结果直接丢掉，只渲染最新的
                item = self._q.get_nowait()
            except queue.Empty:
                break
        if item is None:
            self.root.after(30, self._pump)
            return
        self._pumping = False
        self._on_searched(*item)

    def _on_searched(self, total, rows, dt, err):
        self.btn_search.configure(state="normal", text="检索")
        for i in self.tree.get_children():
            self.tree.delete(i)
        if err:
            self._set_status("查询失败：%s" % err)
            self.q_total = 0
            self._render_pager()
            return
        for r in rows:
            prev = r["body"].replace("\n", "／")
            if len(prev) > 60:
                prev = prev[:60] + "…"
            self.tree.insert("", "end", iid=str(r["id"]), values=(
                r["id"], S.DYNASTY_LABEL.get(r["dynasty"], r["dynasty"]),
                S.KIND_LABEL.get(r["kind"], r["kind"]),
                r["author"] or "佚名", r["title"], prev, ""))
        # 批量取本页核对状态，打勾
        if self.chk_con and rows:
            vmap = ST.checked_batch(self.chk_con, [r["id"] for r in rows])
            for r in rows:
                if vmap.get(r["id"]):
                    self.tree.set(str(r["id"]), "chk", "✓")
        self.q_total = total
        self._render_pager()
        self._set_status("命中 %d 条，本页 %d 条，耗时 %.0f ms" % (total, len(rows), dt))
        if rows:
            self.tree.selection_set(str(rows[0]["id"]))
            self.show_detail()

    def _render_pager(self):
        """根据 q_total / q_page / q_size 刷新分页标签与按钮可用性。"""
        size = self.q_size
        total = self.q_total
        pages = max(1, (total + size - 1) // size) if total else 1
        self.pg_label.configure(
            text="第 %d / %d 页（共 %d 条）" % (self.q_page, pages, total))
        dis = "disabled"
        self.btn_prev.configure(state=dis if self.q_page <= 1 else "normal")
        self.btn_first.configure(state=dis if self.q_page <= 1 else "normal")
        self.btn_next.configure(state=dis if self.q_page >= pages else "normal")
        self.btn_last.configure(state=dis if self.q_page >= pages else "normal")

    def goto_page(self, n: int):
        """翻到第 n 页（沿用上次查询条件）。n 越界会被夹到 [1, pages]。"""
        if not self.q_args:
            return
        self.pg_jump.set(max(1, n))
        self.do_search(max(1, n))

    def show_detail(self):
        sel = self.tree.selection()
        if not sel or not self.con:
            return
        pid = int(sel[0])
        self.cur_pid = pid
        r = Q.get_by_id(self.con, pid)
        if not r:
            self._detail_text("未找到 id=%d" % pid)
            return
        self.cur_aid = r.get("author_id", 0)
        # 当前诗词的核对状态（独立 sidecar，不影响检索与构建）
        self.cur_verified = bool(self.chk_con and ST.get_checked(self.chk_con, pid))
        self.btn_chk.configure(text="取消核对" if self.cur_verified else "标记已核对")
        lines = []
        lines.append("id=%d   [%s/%s]   score=%d  字数=%d  来源=%s"
                     % (r["id"], S.DYNASTY_LABEL.get(r["dynasty"], r["dynasty"]),
                        S.KIND_LABEL.get(r["kind"], r["kind"]),
                        r["score"], r["n_char"], r["src"]))
        adyn = S.DYNASTY_LABEL.get(r.get("author_dynasty") or "", r.get("author_dynasty") or "")
        lines.append("《%s》  %s%s%s" % (
            r["title"], r["author"] or "佚名",
            (" ［%s］" % adyn) if adyn else "",
            ("  [%s]" % r["rhythmic"]) if r["rhythmic"] else ""))
        if r.get("author_desc"):
            lines.append("作者小传：" + r["author_desc"][:300])
        lines.append("")
        body_lines = r["body"].split("\n")
        st = r.get("strains_text") or ""
        if st:
            # 平仄与正文逐字对应，按行切分时用各行字数对齐
            pos = 0
            for bl in body_lines:
                seg = st[pos:pos + len(bl)]
                pos += len(bl)
                lines.append(bl)
                lines.append(seg)
        else:
            lines.extend(body_lines)
        if r["tags"]:
            lines.append("")
            lines.append("标签：" + ", ".join(r["tags"].split("\x1f")))
        if r["notes"]:
            lines.append("注释：" + ", ".join(r["notes"].split("\x1f"))[:400])

        # 扩展资料（独立 JSON sidecar + 本地图片目录，重跑 build.py 不会丢）
        # 按 uid 关联：数字 id 含序号，会因 dedup/重排整体漂移；uid 由内容派生，稳定。
        ex = self.extras.decorated(self.extras.get(r.get("uid") or pid))
        if ex:
            for key, label in XTRA_LABELS:
                v = ex.get(key) or ""
                if v.strip():
                    lines.append("")
                    lines.append("── %s ──" % label)
                    lines.append(v if len(v) <= 1500 else v[:1500] + "…（全文见 extras）")
            imgs = ex.get("images") or []
            if imgs:
                lines.append("")
                lines.append("── 配图 %d 张 ──" % len(imgs))
                for im in imgs:
                    try:
                        miss = "" if os.path.exists(self.extras.image_abs(im)) else "  ← 文件缺失"
                    except Exception:
                        miss = "  ← 非法地址"
                    lines.append("  %s%s" % (im, miss))
            src = " / ".join(x for x in (ex.get("source"), ex.get("license")) if x)
            if src:
                lines.append("")
                lines.append("资料来源：%s" % src)

        lines.append("")
        lines.append("核对状态：%s" % ("已核对 ✓" if self.cur_verified else "未核对"))
        self._detail_text("\n".join(lines))

    # ------------------------------------------------------------ 扩展资料
    def edit_extras(self):
        """
        编辑选中诗词的 译文 / 注释 / 作品简介 / 创作背景 / 赏析 / 配图。

        why 单独一个窗口而不是并入「编辑诗词」：那边改的是订正原始数据、落
        poetry.db；这边是补充资料、落 extras.jsonl ——存储位置、生命周期、
        许可追踪都不同，塞进同一张表单迟早会有人改错地方，且一改就要重写主库。

        两个刻意定下的交互语义：
          - 清空某栏再保存 = 删除该字段（底层 put() 是"非空才覆盖"，
            不放这个语义的话用户根本没法删内容）。
          - 配图框每行一个 URL，http 的会下载到本地 images/ 目录，
            JSONL 里只登记相对地址，图片本体在目录里另行妥善保存。
        """
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先在上方结果里选中一条，再点「扩展资料」")
            return
        pid = int(sel[0])
        # extras 按 uid 关联；老库没有 uid 列时退回数字 id，不至于打不开界面。
        _row = self.con.execute("SELECT uid FROM poems WHERE id=?", (pid,)).fetchone()
        uid = _row[0] if (_row and _row[0]) else pid
        ex = self.extras.decorated(self.extras.get(uid)) or {}

        win = tk.Toplevel(self.root)
        win.title("扩展资料 · id=%d" % pid)
        win.geometry("680x700")
        ttk.Label(win, text="清空某栏再保存 = 删除该字段。配图每行一个地址，"
                            "http 开头的会自动下载到本地 images/ 目录",
                  font=FONT_SM, foreground="#888").pack(anchor="w", padx=8, pady=(8, 2))

        boxes = {}
        for key, label in XTRA_LABELS:
            ttk.Label(win, text=label, font=FONT).pack(anchor="w", padx=8, pady=(6, 0))
            t = scrolledtext.ScrolledText(win, height=4, font=MONO, wrap="word")
            t.pack(fill="both", expand=True, padx=8)
            t.insert("1.0", ex.get(key) or "")
            boxes[key] = t

        ttk.Label(win, text="配图", font=FONT).pack(anchor="w", padx=8, pady=(8, 0))
        imgs = scrolledtext.ScrolledText(win, height=3, font=MONO, wrap="none")
        imgs.pack(fill="x", padx=8)
        imgs.insert("1.0", "\n".join(ex.get("images") or []))

        def save():
            data, cleared = {}, []
            for key, box in boxes.items():
                v = box.get("1.0", "end").strip()
                if v:
                    data[key] = v
                else:
                    cleared.append(X.OUT_KEY_INV[key])
            urls = [x.strip() for x in imgs.get("1.0", "end").splitlines() if x.strip()]
            try:
                rels = []
                for u in urls:
                    if u.lower().startswith(("http://", "https://")):
                        rel, _ = self.extras.save_image(u)
                        rels.append(rel)
                    else:
                        rels.append(u)
                if rels:
                    data["im"] = rels
                else:
                    cleared.append("im")
                if data:
                    self.extras.put(uid, data, source="manual", lic="user")
                for short in cleared:
                    self.extras.remove_field(uid, short)
                self.extras.flush()
                self.extras.write_manifest()
            except Exception as e:
                messagebox.showerror("保存失败", str(e), parent=win)
                return
            win.destroy()
            self.show_detail()
            self._set_status("已保存 id=%d 的扩展资料（%d 个字段）" % (pid, len(data)))

        bb = ttk.Frame(win)
        bb.pack(fill="x", padx=8, pady=10)
        ttk.Button(bb, text="保存", command=save).pack(side="right")
        ttk.Button(bb, text="取消", command=win.destroy).pack(side="right", padx=6)

    def _detail_text(self, s: str):
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", s)
        self.detail.configure(state="disabled")

    # ------------------------------------------------------------ 编辑 / 删除
    def edit_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先在上方结果里选中一条，再点「编辑选中」")
            return
        self.open_editor(int(sel[0]))

    def edit_mine(self):
        sel = self.mine.selection()
        if not sel:
            messagebox.showinfo("提示", "先在下方「我的条目」里选中一条，再点「编辑选中」")
            return
        self.open_editor(int(sel[0]))

    def del_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先在上方结果里选中一条，再点「删除选中」")
            return
        self.delete_by_pid(int(sel[0]))

    def delete_by_pid(self, pid: int, force: bool = False):
        if not messagebox.askyesno("确认删除", "确定删除 id=%d 吗？此操作不可撤销。" % pid):
            return
        try:
            ST.delete_poem(self.con, pid, force=force)
        except ST.Rejected as e:
            # 构建期原始数据默认删不掉——这里再给一次"强制删除"的机会。
            if force or not messagebox.askyesno(
                    "注意", "%s\n仍要强制删除这条原始数据吗？\n（删除后只能重建数据库恢复）" % e):
                messagebox.showwarning("未删除", str(e))
                return
            try:
                ST.delete_poem(self.con, pid, force=True)
            except ST.Rejected as e2:
                messagebox.showwarning("未删除", str(e2))
                return
        if str(pid) in self.tree.get_children():
            self.tree.delete(str(pid))
        self.refresh_mine()
        self._refresh_stats()
        self._set_status("已删除 id=%d" % pid)

    def toggle_checked(self):
        """标记 / 取消「已核对」。写入独立 sidecar 库，不受重建影响。"""
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在上方结果里选中一条诗词，再标记核对状态")
            return
        pid = int(sel[0])
        if not self.chk_con:
            try:
                self.chk_con = ST.open_checks(self.chk_db, rw=True)
            except Exception as e:
                messagebox.showerror("错误", "核对库打开失败：%s" % e)
                return
        cur = ST.get_checked(self.chk_con, pid)
        ST.set_checked(self.chk_con, pid, not cur)
        self.tree.set(str(pid), "chk", "✓" if not cur else "")
        self.show_detail()
        self._refresh_stats()
        self._set_status("已%s id=%d" % ("标记为已核对" if not cur else "取消核对", pid))

    # ------------------------------------------------------------ 作者档案编辑
    def edit_author_of_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先在上方结果里选中一条，再点「编辑作者」")
            return
        pid = int(sel[0])
        row = self.con.execute("SELECT author_id FROM poems WHERE id=?", (pid,)).fetchone()
        if not row or not row[0]:
            messagebox.showinfo("提示", "该诗作者为佚名（无作者记录），暂不支持编辑作者简介")
            return
        self.open_author_editor(row[0])

    def open_author_editor(self, aid: int):
        """编辑作者小传与朝代（不改诗词本身的朝代/体裁，不影响检索与平仄关联）。"""
        if not self.con:
            messagebox.showerror("错误", "数据库未打开：%s" % self.err)
            return
        row = self.con.execute(
            "SELECT id,name,dynasty,desc FROM authors WHERE id=?", (aid,)).fetchone()
        if row is None:
            messagebox.showerror("错误", "未找到作者 id=%d" % aid)
            return

        win = tk.Toplevel(self.root)
        win.title("编辑作者  %s" % row["name"])
        win.geometry("480x460")
        win.minsize(380, 360)

        v_name = tk.StringVar(value=row["name"])
        dkey = row["dynasty"] if row["dynasty"] in S.DYNASTY else "custom"
        v_dyn = tk.StringVar(value=S.DYNASTY_LABEL[dkey])

        f = ttk.Frame(win)
        f.pack(fill="x", padx=10, pady=8)
        ttk.Label(f, text="作者名", font=FONT).grid(row=0, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Entry(f, textvariable=v_name, font=FONT, width=26).grid(row=0, column=1, sticky="w", pady=3)
        ttk.Label(f, text="朝代", font=FONT).grid(row=1, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Combobox(f, textvariable=v_dyn, width=8, font=FONT, state="readonly",
                     values=[S.DYNASTY_LABEL[k] for k in S.DYNASTY]
                     ).grid(row=1, column=1, sticky="w", pady=3)

        ttk.Label(win, text="简介 / 作者小传", font=FONT).pack(anchor="w", padx=10, pady=(4, 0))
        desc = scrolledtext.ScrolledText(win, height=16, font=FONT, wrap="word")
        desc.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        desc.insert("1.0", row["desc"] or "")

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=10, pady=6)

        def _dyn_label_to_key(lab):
            for k, v in S.DYNASTY_LABEL.items():
                if v == lab:
                    return k
            return "custom"

        def do_save():
            try:
                ST.update_author(self.con, aid, name=v_name.get(),
                                 dynasty=_dyn_label_to_key(v_dyn.get()),
                                 desc=desc.get("1.0", "end").strip())
            except ST.Rejected as e:
                messagebox.showwarning("未保存", str(e))
                return
            messagebox.showinfo("已保存", "作者《%s》信息已更新" % v_name.get())
            win.destroy()
            self.show_detail()
            self._refresh_stats()

        ttk.Button(btns, text="保存", command=do_save).pack(side="left")
        ttk.Button(btns, text="取消", command=win.destroy).pack(side="left", padx=8)
        ttk.Label(btns, text="改的是作者档案，不会动到诗词本身的朝代/体裁",
                  font=FONT_SM, foreground="#888").pack(side="left", padx=10)
        win.transient(self.root)
        win.grab_set()
        win.wait_visibility()
        desc.focus_set()

    # ------------------------------------------------------------ 右键菜单
    def _build_context_menu(self):
        self.ctx = tk.Menu(self.root, tearoff=0)
        self.ctx.add_command(label="编辑诗词", command=self.edit_selected)
        self.ctx.add_command(label="编辑作者", command=self.edit_author_of_selected)
        self.ctx.add_command(label="扩展资料", command=self.edit_extras)
        self.ctx.add_command(label="标记已核对/取消", command=self.toggle_checked)
        self.ctx.add_separator()
        self.ctx.add_command(label="删除", command=self.del_selected)
        self.tree.bind("<Button-3>", self._on_tree_rightclick)

    def _on_tree_rightclick(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        self.show_detail()
        try:
            self.ctx.tk_popup(event.x_root, event.y_root)
        finally:
            self.ctx.grab_release()

    # ------------------------------------------------------------ 写回源语料
    def _built_at(self) -> str:
        """这份库是哪一次构建出来的。写回前拿它复核「编辑期间库有没有被重建」。"""
        try:
            row = self.con.execute("SELECT v FROM meta WHERE k='built_at'").fetchone()
            return row[0] if row else ""
        except Exception:
            return ""

    def _writeback(self, pid: int, old_row: dict, new_row: dict) -> str:
        """保存后把改动同步回源语料 JSON，返回附在保存提示里的一段说明。

        写回只是「顺手同步」：数据库那一半已经提交了，这里无论如何都不回滚，
        只把结果如实说清楚——写成功了／不用写／为什么没写。
        """
        if self._src_loc is None:
            self._src_loc = SourceLocator(self.con)
        loc = self._src_loc
        if loc.err:
            return "源语料未写回：%s" % loc.err
        t0 = time.time()
        self._set_status("正在定位…" if loc.warm else "首次保存要建一次源文件索引，稍等…")
        self.root.update_idletasks()
        try:
            hit = loc.locate(pid)
        finally:
            self._set_status("")
        if not hit:
            # 只看 src_id 认不出「自建」：自建条目被改过一次之后，update_poem 会把
            # 来源翻成 user-edit，和「被改过的语料条目」再没区别。号段不会变——
            # 自建条目的朝代位恒为 custom（store.add_poem 的约定，见 schema.DYNASTY）。
            if ST.is_user_added(self.con, pid) or new_row["dynasty"] == "custom":
                return "源语料未写回：这条是自建条目，语料里没有对应记录"
            return ("源语料未写回：语料里找不到这条记录（构建期可能被去重丢弃，"
                    "或语料目录与这份库对不上）")
        try:
            res = writeback_record(self.con, pid, old_row, new_row, hit)
        except Exception as e:                 # 写文件出岔子不能连累已经提交的库
            return "源语料未写回（%s: %s）" % (type(e).__name__, e)
        if res["level"] == "written":
            return "源语料已同步：%s（%.1fs）\n备份：%s" % (
                res["msg"], time.time() - t0, _short_path(res.get("backup") or ""))
        if res["level"] == "noop":
            return "源语料无需写回：%s" % res["msg"]
        if res["level"] == "skip":
            return "源语料未动：%s" % res["msg"]
        return "源语料未写回：%s" % res["msg"]

    def open_editor(self, pid: int):
        """弹出修订窗口，预填当前记录；保存时走 store.update_poem 写回数据库。"""
        if not self.con:
            messagebox.showerror("错误", "数据库未打开：%s" % self.err)
            return
        rec = Q.get_by_id(self.con, pid)
        if not rec:
            messagebox.showerror("错误", "未找到 id=%d" % pid)
            return

        win = tk.Toplevel(self.root)
        win.title("编辑诗词  id=%d" % pid)
        win.geometry("600x680")
        win.minsize(460, 520)

        v_title = tk.StringVar(value=rec["title"])
        v_author = tk.StringVar(value=rec["author"] or "")
        v_rhy = tk.StringVar(value=rec["rhythmic"] or "")
        v_tags = tk.StringVar(value=", ".join(rec["tags"].split("\x1f")) if rec["tags"] else "")
        v_notes = tk.StringVar(value=", ".join(rec["notes"].split("\x1f")) if rec["notes"] else "")
        v_wb = tk.BooleanVar(value=True)          # 同时写回源 JSON（默认开）
        built_at = self._built_at()               # 本次编辑所依据的库是哪一次构建的
        dyn_lab = S.DYNASTY_LABEL.get(rec["dynasty"], rec["dynasty"])
        kind_lab = S.KIND_LABEL.get(rec["kind"], rec["kind"])

        f = ttk.Frame(win)
        f.pack(fill="x", padx=10, pady=8)
        field_rows = [("标题", v_title), ("作者", v_author), ("词牌/曲牌", v_rhy),
                      ("标签(逗号分隔)", v_tags), ("注释(逗号分隔)", v_notes)]
        for i, (lab, var) in enumerate(field_rows):
            ttk.Label(f, text=lab, font=FONT).grid(row=i, column=0, sticky="e", padx=(0, 6), pady=3)
            ttk.Entry(f, textvariable=var, font=FONT, width=42).grid(row=i, column=1, sticky="w", pady=3)
        ttk.Label(f, text="朝代", font=FONT).grid(row=5, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Label(f, text=dyn_lab + "（不可改，改动需重建）", font=FONT_SM, foreground="#666").grid(
            row=5, column=1, sticky="w", pady=3)
        ttk.Label(f, text="体裁", font=FONT).grid(row=6, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Label(f, text=kind_lab + "（不可改，保留 id 以维持平仄关联）", font=FONT_SM, foreground="#666").grid(
            row=6, column=1, sticky="w", pady=3)

        ttk.Label(win, text="正文（一行一句，空行忽略）", font=FONT).pack(anchor="w", padx=10, pady=(4, 0))
        body = scrolledtext.ScrolledText(win, height=16, font=FONT, wrap="word")
        body.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        body.insert("1.0", rec["body"])

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=10, pady=6)

        def do_save():
            raw = body.get("1.0", "end")
            lines = [x.strip() for x in raw.splitlines() if x.strip()]
            tags = [x.strip() for x in v_tags.get().split(",") if x.strip()]
            notes = [x.strip() for x in v_notes.get().split(",") if x.strip()]
            # 数据库这一半先行：写源 JSON 是「顺手同步」，失败绝不回滚库
            old_row = Q.get_by_id(self.con, pid)
            try:
                ST.update_poem(self.con, pid, title=v_title.get(), author=v_author.get(),
                               lines=lines, rhythmic=v_rhy.get(), tags=tags, notes=notes)
            except ST.Rejected as e:
                messagebox.showwarning("未保存", str(e))
                return
            except Exception as e:            # 落库异常不能让窗口卡死
                messagebox.showerror("写入失败", "%s: %s" % (type(e).__name__, e))
                return
            new_row = Q.get_by_id(self.con, pid)
            extra = ""
            if v_wb.get():
                if built_at and self._built_at() != built_at:
                    # 窗口开着的时候库被重建过，id→源文件的对应关系未必还成立
                    extra = "\n源语料未写回：数据库在编辑期间被重新构建过，"\
                            "请重新打开这条记录再改（数据库已更新）"
                elif old_row and new_row:
                    extra = "\n" + self._writeback(pid, old_row, new_row)
            messagebox.showinfo("已保存", "id=%d 《%s》已修订并写回数据库%s"
                                % (pid, v_title.get(), extra))
            win.destroy()
            if str(pid) in self.tree.get_children():
                self.tree.selection_set(str(pid))
                self.show_detail()
            self.refresh_mine()
            self._refresh_stats()

        ttk.Button(btns, text="保存", command=do_save).pack(side="left")
        ttk.Button(btns, text="取消", command=win.destroy).pack(side="left", padx=8)
        ttk.Checkbutton(btns, text="同时写回源 JSON", variable=v_wb).pack(side="left", padx=(12, 0))
        ttk.Label(btns, text='修改会标记为"用户修订"，可重新检索', font=FONT_SM,
                  foreground="#888").pack(side="left", padx=10)
        win.transient(self.root)
        win.grab_set()
        win.wait_visibility()
        body.focus_set()

    # ============================================================ 添加窗口
    def _build_add(self, parent):
        form = ttk.Frame(parent)
        form.pack(fill="x", padx=8, pady=6)

        self.v_t_title = tk.StringVar()
        self.v_t_author = tk.StringVar()
        self.v_t_rhy = tk.StringVar()
        self.v_t_tags = tk.StringVar()
        self.v_t_dyn = tk.StringVar(value=S.DYNASTY_LABEL["custom"])
        self.v_t_kind = tk.StringVar(value=S.KIND_LABEL["poem"])

        rows = [
            ("标题", self.v_t_title, 0, 0, 30),
            ("作者", self.v_t_author, 0, 2, 16),
            ("朝代", None, 0, 4, 10),
            ("体裁", None, 1, 4, 10),
            ("词牌/曲牌", self.v_t_rhy, 1, 0, 30),
            ("标签(逗号分隔)", self.v_t_tags, 1, 2, 16),
        ]
        for lab, var, r, c, w in rows:
            ttk.Label(form, text=lab, font=FONT).grid(row=r, column=c, sticky="e", padx=(0, 4), pady=3)
            if var is None:
                continue
            ttk.Entry(form, textvariable=var, font=FONT, width=w).grid(row=r, column=c + 1, sticky="w", pady=3)
        ttk.Combobox(form, textvariable=self.v_t_dyn, width=8, font=FONT, state="readonly",
                     values=[S.DYNASTY_LABEL[k] for k in S.DYNASTY]
                     ).grid(row=0, column=5, sticky="w", pady=3)
        ttk.Combobox(form, textvariable=self.v_t_kind, width=8, font=FONT, state="readonly",
                     values=[S.KIND_LABEL[k] for k in S.KIND]
                     ).grid(row=1, column=5, sticky="w", pady=3)

        ttk.Label(parent, text="正文（一行一句，空行忽略）", font=FONT).pack(anchor="w", padx=8)
        self.body_text = scrolledtext.ScrolledText(parent, height=8, font=FONT, wrap="word")
        self.body_text.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        btns = ttk.Frame(parent)
        btns.pack(fill="x", padx=8, pady=4)
        ttk.Button(btns, text="提交", command=self.do_add).pack(side="left")
        ttk.Button(btns, text="清空表单", command=self._clear_form).pack(side="left", padx=6)
        ttk.Label(btns, text="提交后立即可被检索到；繁体会自动转简体、空格自动去除",
                  font=FONT_SM, foreground="#888").pack(side="left", padx=12)

        ttk.Label(parent, text="我的条目（自建 / 修订，可编辑或删除）", font=FONT).pack(anchor="w", padx=8, pady=(8, 0))
        box = ttk.Frame(parent)
        box.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.mine = ttk.Treeview(box, columns=("id", "title", "author", "n", "src"),
                                 show="headings", height=6)
        for c, (t, w) in {"id": ("id", 100), "title": ("标题", 230),
                          "author": ("作者", 130), "n": ("字数", 60),
                          "src": ("来源", 90)}.items():
            self.mine.heading(c, text=t)
            self.mine.column(c, width=w, anchor="w")
        vsb = ttk.Scrollbar(box, orient="vertical", command=self.mine.yview)
        self.mine.configure(yscrollcommand=vsb.set)
        self.mine.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        bb = ttk.Frame(parent)
        bb.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(bb, text="刷新", command=self.refresh_mine).pack(side="left")
        ttk.Button(bb, text="编辑选中", command=self.edit_mine).pack(side="left", padx=6)
        ttk.Button(bb, text="删除选中", command=self.do_delete).pack(side="left", padx=6)

    def _clear_form(self):
        for v in (self.v_t_title, self.v_t_author, self.v_t_rhy, self.v_t_tags):
            v.set("")
        self.body_text.delete("1.0", "end")

    def _t_dyn_key(self) -> str:
        lab = self.v_t_dyn.get()
        for k, v in S.DYNASTY_LABEL.items():
            if v == lab:
                return k
        return "custom"

    def _t_kind_key(self) -> str:
        lab = self.v_t_kind.get()
        for k, v in S.KIND_LABEL.items():
            if v == lab:
                return k
        return "poem"

    def do_add(self):
        if not self.con:
            messagebox.showerror("错误", "数据库未打开：%s" % self.err)
            return
        raw = self.body_text.get("1.0", "end")
        lines = [x.strip() for x in raw.splitlines()]
        lines = [x for x in lines if x]
        tags = [x.strip() for x in self.v_t_tags.get().split(",") if x.strip()]
        try:
            r = ST.add_poem(self.con, dynasty=self._t_dyn_key(), kind=self._t_kind_key(),
                            title=self.v_t_title.get(), author=self.v_t_author.get(),
                            lines=lines, rhythmic=self.v_t_rhy.get(), tags=tags)
        except ST.Rejected as e:
            messagebox.showwarning("未提交", str(e))
            return
        except Exception as e:
            messagebox.showerror("写入失败", "%s: %s" % (type(e).__name__, e))
            return
        self._clear_form()
        self.refresh_mine()
        self._refresh_stats()
        self._set_status("已写入 id=%d 《%s》（%d 字）" % (r["id"], r["title"], r["n_char"]))

    def refresh_mine(self):
        if not self.con:
            return
        for i in self.mine.get_children():
            self.mine.delete(i)
        for r in ST.list_mine(self.con, 500):
            src_lab = "修订" if r["src"] == ST.EDIT_SRC else "自建"
            self.mine.insert("", "end", iid=str(r["id"]),
                             values=(r["id"], r["title"], r["author"] or "佚名",
                                     r["n_char"], src_lab))

    def do_delete(self):
        sel = self.mine.selection()
        if not sel:
            messagebox.showinfo("提示", "先在下方「我的条目」里选中一条")
            return
        self.delete_by_pid(int(sel[0]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=_default(os.path.join("dist", "poetry.db")))
    ap.add_argument("--strains-db", default=_default(os.path.join("dist", "poetry-strains.db")))
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit("找不到数据库：%s\n先跑：python build.py --out ./dist" % args.db)

    root = tk.Tk()
    style = ttk.Style()
    if "vista" in style.theme_names():
        style.theme_use("vista")
    App(root, args.db, args.strains_db)
    root.mainloop()


if __name__ == "__main__":
    main()
