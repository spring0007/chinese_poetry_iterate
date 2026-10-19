# -*- coding: utf-8 -*-
"""
写入层：新增 / 删除条目。GUI(app.py)、HTTP 接口(examples/api_server.py)、CLI 共用这一层。

why 单独一层而不是让 GUI 直接拼 SQL：
  1. 新增条目要同时维护 poems / authors / rhythmics / sources 四张表和 id 号段，
     任何一个地方漏了都会留下"查得到正文但按作者搜不到"的半成品数据。
  2. 删除是危险操作，必须在一处强制校验"只能删自己加的"，不能靠每个调用方自觉。

设计约定：
  - 新增条目与构建期产物共用 poems 表，靠 id 号段区分：
    next_id() 取该 (dynasty,kind) 下当前最大 seq + 1，因此永远不会撞已有 id。
    默认 dynasty=custom（号段起点 10,000,000，与批量产物明显错开）。
  - 新增条目的 src 固定为 "user-added"。删除时只认这个标记，
    构建期导入的 34 万条一概删不掉——即使调用方传了它的 id。
  - 所有文本走 schema.norm_text / norm_author，与构建期完全同一套规则，
    保证"新增的内容"和"导入的内容"检索行为一致（繁体、空格都会被归一）。
"""

from __future__ import annotations
import sqlite3, sys, os, time, json
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema as S

USER_SRC = "user-added"
# 被用户修订过的构建期数据（含正文/作者订正）统一挂这个来源标记。
# why 单独一个来源而不是复用 user-added：二者语义不同——
#   user-added 是完全新增、源码里没有的数据；user-edit 是"改了构建期导入的某条"。
# 用不同标记才能在重建/导出时分别处理，也方便在 UI 里区分"我录的"和"我改的"。
EDIT_SRC = "user-edit"
# 核对状态库（独立 sidecar）：每条诗词是否被"核对过"是纯用户标注，
# 与从源码重建出来的内容无关，所以单独一个文件存——重跑 build.py 重建 poetry.db
# 时完全碰不到它，核对标注天然不丢。
CHECKS_DB = "poetry-checks.db"


def _here(name: str) -> str:
    """默认库路径按脚本位置解析，不依赖当前工作目录。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist", name)


class Rejected(Exception):
    """输入不合法。带明确原因，UI 直接显示给用户。"""


def meta_version(con, db: str = "main") -> str:
    """读某个库里的 schema_version。库里没有 meta 表（比如挂错了文件）时返回空串。"""
    try:
        row = con.execute("SELECT v FROM %s.meta WHERE k='schema_version'" % db).fetchone()
    except sqlite3.OperationalError:
        return ""
    return row[0] if row else ""


def check_version(con, path: str, db: str = "main"):
    """
    主版本不一致就直接拒绝，不静默继续。

    why：v3 把 poem_strains 移出了主库，而 v2 的库里有这张表、v1 还有 body_s 列。
    不校验的话症状会非常隐蔽——比如拿 v2 的库配 v3 代码，平仄能查到（表在），
    但凡是依赖"平仄是独立包"的分发逻辑全错；反过来更糟，直接 KeyError。
    只比主版本号：次版本（3.0 -> 3.1）是向后兼容的增量，不该拦。
    """
    v = meta_version(con, db)
    if not v:
        return
    if v.split(".")[0] != S.SCHEMA_VERSION.split(".")[0]:
        raise Rejected(
            "数据库 %s 的 schema 版本是 %s，当前代码要求 %s —— 请用当前版本重新 build.py "
            "（跨主版本的库结构不兼容，继续用会出现查得到但语义错的问题）"
            % (path, v, S.SCHEMA_VERSION))


def open_ro(path: str, strains_db: str = "") -> sqlite3.Connection:
    """
    只读连接。why 单独一个函数：sqlite3 默认禁止跨线程复用连接
    （"objects created in a thread can only be used in that same thread"），
    而检索要放到子线程里做（否则界面会卡住）。所以子线程各开各的连接，
    并且用 mode=ro 打开——检索路径没有任何理由需要写权限。
    """
    if not os.path.exists(path):
        raise Rejected("数据库不存在：%s（先跑 build.py）" % path)
    from urllib.request import pathname2url
    con = sqlite3.connect("file:" + pathname2url(os.path.abspath(path)) + "?mode=ro",
                          uri=True)
    con.row_factory = sqlite3.Row
    check_version(con, path)
    if strains_db and os.path.exists(strains_db):
        con.execute("ATTACH DATABASE ? AS strainsdb", (strains_db,))
    return con


def open_rw(path: str, strains_db: str = "") -> sqlite3.Connection:
    if not os.path.exists(path):
        raise Rejected("数据库不存在：%s（先跑 build.py）" % path)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    check_version(con, path)
    require_writable_schema(con, path)
    if strains_db and os.path.exists(strains_db):
        con.execute("ATTACH DATABASE ? AS strainsdb", (strains_db,))
    return con


def _has_table(con, name: str) -> bool:
    return con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
                       (name,)).fetchone()[0] > 0


def _has_column(con, table: str, col: str) -> bool:
    return any(r[1] == col for r in con.execute("PRAGMA table_info(%s)" % table))


def require_writable_schema(con, path: str) -> None:
    """写路径必须确认主库是当前 schema（3.4）的形态，缺什么一次性说清楚。

    why 单独一步：缺少列时 sqlite 抛的是 "NOT NULL constraint failed: poems.uid"
    这种**指向症状而不是病因**的错误，调用方（GUI）只能原样弹给用户看。
    3.1 时代 store.py 就是这么坏的——诗查得到、却一条也写不进去，报的还是一句
    看不懂的约束名。这里把"库版本旧了，重跑 build.py"直接讲明白。
    """
    miss = []
    if not _has_column(con, "poems", "uid"):
        miss.append("缺 poems.uid")
    if not _has_column(con, "poems", "author_uid"):
        miss.append("缺 poems.author_uid")
    if not _has_column(con, "authors", "uid"):
        miss.append("缺 authors.uid")
    if not _has_table(con, "poem_extras"):
        miss.append("缺 poem_extras 表")
    if _has_column(con, "poems", "notes"):
        # v3.3 把这一列删了。还留着它的库说明写路径没跟上：继续往里写就会退回
        # "注释有两个家"的状态，而读侧（query.py）已经只认 poem_extras.notes。
        miss.append("多了 poems.notes 列（v3.3 已移除）")
    if _has_column(con, "poems", "author_id"):
        # v3.4 把这一列删了。留着它本身不会写错数据，但会诱使调用方继续按 id
        # 关联作者，而这里写进去的 author_id 会随 authors 表重建整体位移——
        # 正是这次要消灭的那个坑。宁可直接拦下来让人重建一次。
        miss.append("多了 poems.author_id 列（v3.4 已移除，作者关联请用 author_uid）")
    if miss:
        raise Rejected(
            "数据库 %s 的结构与当前写路径不匹配（%s）——它是旧版本的产物；"
            "请用当前版本重跑 build.py 生成新库（旧库可只读使用）"
            % (path, "；".join(miss)))


# ---------------------------------------------------------------- 号段
def next_id(con, dynasty: str, kind: str) -> int:
    """该 (dynasty,kind) 下下一个可用 id。取现有最大 seq+1，绝不复用已有 id。"""
    if dynasty not in S.DYNASTY:
        raise Rejected("未知朝代 %r，可选：%s" % (dynasty, "/".join(sorted(S.DYNASTY))))
    if kind not in S.KIND:
        raise Rejected("未知体裁 %r，可选：%s" % (kind, "/".join(sorted(S.KIND))))
    lo = S.DYNASTY[dynasty] << (S.KIND_BITS + S.SEQ_BITS)
    hi = lo + (1 << (S.KIND_BITS + S.SEQ_BITS)) - 1
    row = con.execute(
        "SELECT MAX(id) FROM poems WHERE id BETWEEN ? AND ? AND ((id>>?)&7)=?",
        (lo, hi, S.SEQ_BITS, S.KIND[kind])).fetchone()
    mx = row[0] if row else None
    if mx is None:
        seq = S.CUSTOM_SEQ_BASE if dynasty == "custom" else 0
    else:
        seq = (mx & S.SEQ_MAX) + 1
        if dynasty == "custom" and seq < S.CUSTOM_SEQ_BASE:
            seq = S.CUSTOM_SEQ_BASE
    if seq > S.SEQ_MAX:
        raise Rejected("%s/%s 号段已用尽（seq > %d）" % (dynasty, kind, S.SEQ_MAX))
    return S.make_id(dynasty, kind, seq)


# ---------------------------------------------------------------- 字典表维护
def _ensure_dict(con, table: str, name: str) -> int:
    """字典表取 id，没有就插入。authors 表没有 UNIQUE 约束（同名可跨朝代），
    所以必须先查再插，不能直接 INSERT OR IGNORE。"""
    row = con.execute("SELECT id FROM %s WHERE name=?" % table, (name,)).fetchone()
    if row:
        return row[0]
    cur = con.execute("INSERT INTO %s(name) VALUES(?)" % table, (name,))
    return cur.lastrowid


def ensure_author(con, name: str, dynasty: str) -> int:
    """保证作者行存在，返回 author_id（authors 表主键；poems 已不存这一列，
    调用方通常只需要它来维护 n_poems 计数）。"""
    return ensure_author_row(con, name, dynasty)[0]


def ensure_author_row(con, name: str, dynasty: str) -> tuple:
    """保证作者行存在，返回 (author_id, author_uid)。

    同名跨朝代的作者在库里并存（实测 275 组，如 严武 / 乐史 唐宋各一条），
    这里**取 id 最大的那条**：build.py 里 author_uids 是 {name: 最后一条} 的
    "同名后者覆盖前者"，而 authors 行是按列表顺序、id 递增插入的，所以"最大 id"
    就是构建期真正被诗词引用的那一行。两边选行口径一致，重建前后才不会换人。

    why 仍然返回 author_id：v3.4 起 poems 已经不存这一列（作者关联只走
    author_uid），但它仍是 authors 表的主键，写 authors.n_poems 计数时要用。
    """
    row = con.execute(
        "SELECT id, uid FROM authors WHERE name=? ORDER BY id DESC LIMIT 1",
        (name,)).fetchone()
    if row:
        return row[0], row[1]
    uid = S.make_author_uid(name, dynasty)
    cur = con.execute(
        "INSERT INTO authors(uid,name,dynasty,desc,n_poems) VALUES(?,?,?,?,0)",
        (uid, name, dynasty, ""))
    return cur.lastrowid, uid


def set_notes(con, uid: str, notes) -> None:
    """把注释写进 poem_extras —— v3.3 起，注释的**唯一**存放处。

    why 不写 poems.notes：那一列已经在 v3.3 删掉了（构建期源语料的注释也是直接
    落这张表）。再往主库写就变成同一个字段有两个家，query.py 又得回去写 COALESCE，
    而"两处都能写"正是 v3.2 丢过一次数据的结构性原因。
    notes 传空列表 = 显式清空注释。
    """
    text = "\n".join(str(x) for x in notes) if isinstance(notes, (list, tuple)) else (notes or "")
    text = text.strip()
    if text:
        # updated 是 Unix 整数秒（v3.3 起），与列声明的 INTEGER 一致：
        # 存成 ISO 文本会让 `WHERE updated > ?` 静默失效。
        con.execute(
            "INSERT INTO poem_extras(uid,notes,updated) VALUES(?,?,?) "
            "ON CONFLICT(uid) DO UPDATE SET notes=excluded.notes,updated=excluded.updated",
            (uid, text, int(time.time())))
    else:
        con.execute("UPDATE poem_extras SET notes='' WHERE uid=?", (uid,))


def notes_of(con, uid: str) -> list:
    """读回注释（按行拆开），供 CLI 在未指定字段时当默认值。"""
    row = con.execute("SELECT notes FROM poem_extras WHERE uid=?", (uid,)).fetchone()
    if not row or not row[0]:
        return []
    return [x for x in row[0].split("\n") if x.strip()]


def ensure_src(con) -> int:
    return _ensure_dict(con, "sources", USER_SRC)


# ---------------------------------------------------------------- 新增
def add_poem(con, *, dynasty: str = "custom", kind: str = "poem",
             title: str = "", author: str = "", lines=None,
             rhythmic: str = "", tags=None, notes=None) -> dict:
    """
    写入一条。返回 {"id":..., "author_id":..., "new_author":bool}。

    校验按"先硬后软"：空正文、未知朝代体裁直接拒绝（否则会写进库里变成脏数据），
    标题缺失则用首行兜底（一条没有标题的记录在 UI 里显示为《》很难用）。
    """
    raw_lines = [str(x).strip() for x in (lines or [])]
    raw_lines = [x for x in raw_lines if x]
    if not raw_lines:
        raise Rejected("正文为空")
    if dynasty not in S.DYNASTY:
        raise Rejected("未知朝代 %r，可选：%s" % (dynasty, "/".join(sorted(S.DYNASTY))))
    if kind not in S.KIND:
        raise Rejected("未知体裁 %r，可选：%s" % (kind, "/".join(sorted(S.KIND))))

    n_title = S.norm_text(title or "")
    n_author = S.norm_author(author or "")
    n_lines = S.norm_lines(raw_lines)
    n_rhythmic = S.norm_text(rhythmic or "")
    if not n_lines:
        raise Rejected("正文归一后为空")
    if not n_title:
        n_title = n_lines[0][:12]
    n_tags = [S.norm_text(t) for t in (tags or []) if S.norm_text(t)]
    n_notes = [S.norm_text(t) for t in (notes or []) if S.norm_text(t)]

    pid = next_id(con, dynasty, kind)
    aid, auid = ensure_author_row(con, n_author, dynasty) if n_author else (0, "")
    rid = _ensure_dict(con, "rhythmics", n_rhythmic) if n_rhythmic else 0
    sid = ensure_src(con)
    body = "\n".join(n_lines)
    # uid 由内容派生（schema.make_uid），必须在这里算出来写进去：poems.uid 是
    # NOT NULL，而且外部数据（extras）就是靠它关联的。author_uid 同理——v3.4 起
    # 它是 poems 关联作者的**唯一**一列（author_id 已从主表移除），必须与
    # authors.uid 指向同一行，写漏了这条诗就会显示成佚名。
    uid = S.make_uid(dynasty, n_author, n_title, body)

    con.execute(
        "INSERT INTO poems(id,uid,author_uid,title,rhythmic_id,src_id,body,tags,"
        "score,n_char) VALUES(?,?,?,?,?,?,?,?,0,?)",
        (pid, uid, auid, n_title, rid, sid, body,
         "\x1f".join(n_tags), sum(len(x) for x in n_lines)))
    # 注释落 poem_extras —— v3.3 起 poems 已经**没有** notes 列，这里不是"留空",
    # 而是主库根本不存注释（理由见 set_notes 的注释）。
    set_notes(con, uid, n_notes)
    if n_author:
        con.execute("UPDATE authors SET n_poems=n_poems+1 WHERE id=?", (aid,))
    con.commit()
    return {"id": pid, "uid": uid, "author_id": aid, "author_uid": auid,
            "new_author": aid != 0,
            "title": n_title, "n_char": sum(len(x) for x in n_lines)}


# ---------------------------------------------------------------- 修改
def update_poem(con, pid: int, *, title: str = "", author: str = "",
                lines=None, rhythmic: str = "", tags=None, notes=None) -> dict:
    """
    就地修订一条已存在的记录，保留 id（id 高位编码朝代/体裁，改了 id 会变，
    会破坏 poetry-strains.db 的平仄关联和既有检索链接，所以这里**绝不**改 id）。

    可改字段：标题 / 作者 / 词牌 / 标签 / 注释 / 正文。朝代、体裁不可改（见上）。
    若某条构建期导入的诗显示有误（错字、作者错挂等），就走这里订正并落库。

    修订后的记录会把来源标记成 EDIT_SRC（原本就是自建的则保持 user-added），
    以便和原始构建数据区分、并在重建前可一并导出备份。

    关于 uid 与 author_uid（schema 3.2 / 3.4）：
      - author_uid 跟随新的作者一起重算，并即时写成 poems 里那一列（v3.4 起
        poems 关联作者只有这一列，写漏就等于这条诗变成佚名）。
      - uid 由 (朝代, 作者, 标题, 正文) 派生，改了这几项就必须重算——不重算
        库里会留下一个和内容对不上的 uid，下次重建立刻变成另一个值。
        重算时 poem_extras 里那一行会一起搬到新 uid 上（译文/赏析不脱钩），
        但 extras/extras-*.jsonl 分片的键不会变（它是权威源），返回值里的
        `uid_changed=True` 就是提示这件事：需要一次按内容重映射，否则重跑
        build.py 时那条扩展资料会变成孤儿行。
      - notes 传 None = 不动注释；传空列表 = 显式清空。
    """
    row = con.execute(
        "SELECT id,uid,author_uid,rhythmic_id,src_id,body,score FROM poems WHERE id=?",
        (pid,)).fetchone()
    if row is None:
        raise Rejected("id=%d 不存在，无法修改" % pid)
    old_uid = row["uid"]
    old_auid, old_rid, old_src = row["author_uid"], row["rhythmic_id"], row["src_id"]
    # 旧作者的 authors.id：n_poems 计数回退要用（主表已经没有 author_id 这一列，
    # 只能按 author_uid 反查那一行）。
    old_aid = 0
    if old_auid:
        a = con.execute("SELECT id FROM authors WHERE uid=?", (old_auid,)).fetchone()
        old_aid = a[0] if a else 0

    if lines is None:                       # 不传正文 = 保留原文（CLI 用）
        raw_lines = (row["body"] or "").split("\n")
    else:
        raw_lines = [str(x).strip() for x in lines]
    raw_lines = [x for x in raw_lines if x]
    if not raw_lines:
        raise Rejected("正文为空，不能把一首诗改成空白")

    n_title = S.norm_text(title or "")
    n_author = S.norm_author(author or "")
    n_lines = S.norm_lines(raw_lines)
    if not n_lines:
        raise Rejected("正文归一后为空")
    if not n_title:
        n_title = n_lines[0][:12]
    n_rhythmic = S.norm_text(rhythmic or "")
    n_tags = [S.norm_text(t) for t in (tags or []) if S.norm_text(t)]
    n_notes = [S.norm_text(t) for t in (notes or []) if S.norm_text(t)]
    body = "\n".join(n_lines)
    n_char = sum(len(x) for x in n_lines)

    # author / rhythmic 重新解析字典 id：与 add_poem 同一套规则，保证检索一致
    dyn = S.parse_id(pid)[0]
    aid, auid = ensure_author_row(con, n_author, dyn) if n_author else (0, "")
    rid = _ensure_dict(con, "rhythmics", n_rhythmic) if n_rhythmic else 0
    new_uid = S.make_uid(dyn, n_author, n_title, body)

    con.execute(
        "UPDATE poems SET uid=?,author_uid=?,title=?,rhythmic_id=?,body=?,"
        "tags=?,n_char=? WHERE id=?",
        (new_uid, auid, n_title, rid, body, "\x1f".join(n_tags), n_char, pid))

    # uid 变了就把 poem_extras 里那一行一起搬过去，否则译文/注释/赏析与这首诗脱钩
    if new_uid != old_uid:
        exists = con.execute("SELECT 1 FROM poem_extras WHERE uid=?", (new_uid,)).fetchone()
        if exists:
            # 目标 uid 上已经有记录（这首诗此前被改回过同一内容）：留下那份、
            # 丢掉旧的一份，否则主键冲突会让整次修订失败。
            con.execute("DELETE FROM poem_extras WHERE uid=?", (old_uid,))
        else:
            con.execute("UPDATE poem_extras SET uid=? WHERE uid=?", (new_uid, old_uid))

    # notes 传 None = 不动注释（CLI edit 不传此参数时保留原文，否则会静默清空注释）；
    # 传空列表 = 显式清空。
    if notes is not None:
        set_notes(con, new_uid, n_notes)

    # 作者计数：作者变了才动，避免无谓写库；旧作者无人引用就回收
    if old_aid != aid:
        if old_aid:
            con.execute("UPDATE authors SET n_poems=MAX(n_poems-1,0) WHERE id=?", (old_aid,))
            left = con.execute("SELECT COUNT(*) FROM poems WHERE author_uid=?",
                               (old_auid,)).fetchone()[0]
            if left == 0:
                con.execute("DELETE FROM authors WHERE id=?", (old_aid,))
        if aid:
            con.execute("UPDATE authors SET n_poems=n_poems+1 WHERE id=?", (aid,))

    # 修订来源标记：原本非自建（即构建期数据被改）时挂 EDIT_SRC；自建的保持 user-added
    if old_src != USER_SRC:
        sid = _ensure_dict(con, "sources", EDIT_SRC)
        con.execute("UPDATE poems SET src_id=? WHERE id=?", (sid, pid))

    con.commit()
    if new_uid != old_uid:
        # 不静默：这次修订让 uid 变了，extras.jsonl 里那条扩展资料还挂旧键上，
        # 重跑 build.py 会变成孤儿行。调用方（GUI / CLI）可据此提示用户。
        print("提示：id=%d 内容已变，uid %s -> %s；extras/extras-*.jsonl 分片里"
              "对应的键需要按内容重映射一次" % (pid, old_uid, new_uid), file=sys.stderr)
    return {"id": pid, "uid": new_uid, "uid_changed": new_uid != old_uid,
            "author_id": aid, "author_uid": auid,
            "title": n_title, "n_char": n_char}


# ---------------------------------------------------------------- 作者档案
def update_author(con, aid: int, *, name=None, dynasty=None, desc=None) -> dict:
    """
    修订作者档案：作者名 / 朝代 / 简介。诗词本身不因此改动——
    作者朝代是独立的元数据（诗词的朝代来自 id 高位，互不干扰），
    改作者简介不会影响检索与平仄关联。

    作者名改了只改 authors 表这一行，所有指向该作者的诗会一起显示新名字
    （它们在 poems.author_uid 上关联，主表不存作者名副本）。

    关于 uid（schema 3.2 / 3.4）：authors.uid 由 (姓名, 朝代) 派生，所以改名/改朝代会
    换 uid；换完必须把 poems.author_uid 级联刷一遍，否则那一列指向的作者标识
    就不存在了（v3.4 起这是主表里**唯一**的作者关联列，漏刷等于这批诗全部变佚名）。
    **但 poems.uid 不会跟着重算**：它内嵌了作者名，改名等于让这一批
    诗的 uid 全部失效——那是"按内容重映射"的活（会连带搬到 extras.jsonl），
    需要时单独跑，不在一次改名里顺手做大范围改写。
    """
    row = con.execute("SELECT id,name,dynasty,uid FROM authors WHERE id=?", (aid,)).fetchone()
    if row is None:
        raise Rejected("作者 id=%d 不存在，无法修改" % aid)
    name = None if name is None else S.norm_text(name)
    if name is not None and not name:
        raise Rejected("作者名不能为空")
    if dynasty is not None and dynasty and dynasty not in S.DYNASTY:
        raise Rejected("未知朝代 %r，可选：%s" % (dynasty, "/".join(sorted(S.DYNASTY))))

    sets, args = [], []
    if name is not None:
        sets.append("name=?")
        args.append(name)
    if dynasty is not None:
        sets.append("dynasty=?")
        args.append(dynasty)
    if desc is not None:
        sets.append("desc=?")
        args.append(S.norm_text(desc))
    if not sets:
        return {"id": aid, "changed": False, "uid": row["uid"]}

    new_uid = S.make_author_uid(name if name is not None else row["name"],
                                dynasty if dynasty is not None else row["dynasty"])
    sets.append("uid=?")
    args.append(new_uid)
    args.append(aid)
    try:
        con.execute("UPDATE authors SET %s WHERE id=?" % ",".join(sets), args)
    except sqlite3.IntegrityError as e:
        # 改成和另一个作者完全同名同朝代 => uid 撞车。这里必须回滚后报明确原因，
        # 直接抛 IntegrityError 的话 GUI 只会显示一句 "UNIQUE constraint failed"。
        con.rollback()
        raise Rejected("改完后与已有作者（姓名=%s，朝代=%s）重复，authors.uid 必须唯一"
                       "（同一个作者请直接编辑那一条）：%s"
                       % (name or row["name"], dynasty or row["dynasty"], e))

    # 作者标识一变就级联刷引用方。v3.4 起 poems 只有 author_uid 一列，
    # 所以 WHERE 也按 author_uid 匹配（旧值）——这也是主表只留一列的好处：
    # 不存在"只刷了一列、另一列还是旧值"的可能。
    n_sync = con.execute("UPDATE poems SET author_uid=? WHERE author_uid=?",
                         (new_uid, row["uid"])).rowcount
    con.commit()
    renamed = name is not None and name != row["name"]
    if renamed and n_sync:
        print("提示：作者 id=%d 已改名，%d 首诗的 poems.uid 内嵌旧作者名、已不再等于"
              "内容哈希；如需恢复 uid 与 extras 的对应关系，需要单独做一次按内容重映射"
              % (aid, n_sync), file=sys.stderr)
    return {"id": aid, "changed": True, "uid": new_uid,
            "author_uid_synced": n_sync, "poems_uid_stale": renamed and n_sync > 0}


# ---------------------------------------------------------------- 删除
def is_user_added(con, pid: int) -> bool:
    row = con.execute(
        "SELECT s.name FROM poems p JOIN sources s ON s.id=p.src_id WHERE p.id=?",
        (pid,)).fetchone()
    return bool(row and row[0] == USER_SRC)


def is_user_origin(con, pid: int) -> bool:
    """自建 + 修订 都算"用户来源"，都可被删除/编辑。"""
    row = con.execute(
        "SELECT s.name FROM poems p JOIN sources s ON s.id=p.src_id WHERE p.id=?",
        (pid,)).fetchone()
    return bool(row and row[0] in (USER_SRC, EDIT_SRC))


def delete_poem(con, pid: int, force: bool = False) -> bool:
    """
    只删用户来源条目（自建 / 修订）。构建期导入且从未被改动的 34 万条默认删不掉——
    这是刻意的安全约束：UI 里误点不该把唐诗删掉，且删了无法从源码外恢复
    （要重新全量构建）。被修订过的构建期数据属于用户改动，允许删（删后即退回
    到"需重建才会恢复的原始版"）。

    force=True 时跳过上述来源校验，用于用户在 GUI 里明确"强制删除原始数据"的二次确认后。
    """
    if not force and not is_user_origin(con, pid):
        raise Rejected("id=%d 不是用户来源条目，拒绝删除（构建期原始数据请重新 build.py 生成）" % pid)
    # 作者计数要跟着回退，否则删几次之后 authors.n_poems 会一直虚高。
    # 作者行本身保留：可能有别的作品还指着他，且重名作者本来就无法区分。
    # 主表只有 author_uid（v3.4），按它反查 authors.id 来改计数。
    row = con.execute("SELECT author_uid FROM poems WHERE id=?", (pid,)).fetchone()
    auid = row[0] if row else ""
    con.execute("DELETE FROM poems WHERE id=?", (pid,))
    if auid:
        aid_row = con.execute("SELECT id FROM authors WHERE uid=?", (auid,)).fetchone()
        aid = aid_row[0] if aid_row else 0
    else:
        aid = 0
    if aid:
        con.execute("UPDATE authors SET n_poems=MAX(n_poems-1,0) WHERE id=?", (aid,))
        # 没有人再引用就顺手删掉，否则每录一个测试作者 authors 表就永久多一行。
        # 只删"零引用"的，有别的作品指向它的作者行一定会留着。
        left = con.execute("SELECT COUNT(*) FROM poems WHERE author_uid=?",
                           (auid,)).fetchone()[0]
        if left == 0:
            con.execute("DELETE FROM authors WHERE id=?", (aid,))
    # 来源行同理：自建条目全删光后 sources 里会留一条没人引用的 'user-added'，
    # 于是 sources 表行数会比构建期多 1（实测 17 -> 18），看着像凭空多了一个数据源。
    # 下次 add_poem 会按需重建，所以这里可以放心回收。
    sid = con.execute("SELECT id FROM sources WHERE name=?", (USER_SRC,)).fetchone()
    if sid:
        left = con.execute("SELECT COUNT(*) FROM poems WHERE src_id=?",
                           (sid[0],)).fetchone()[0]
        if left == 0:
            con.execute("DELETE FROM sources WHERE id=?", (sid[0],))
    con.commit()
    return True


# ---------------------------------------------------------------- 查询自建
def list_user_added(con, limit: int = 200, offset: int = 0) -> List[dict]:
    rows = con.execute(
        "SELECT p.id, p.title, a.name AS author, p.body, p.n_char, "
        "r.name AS rhythmic, p.tags "
        "FROM poems p LEFT JOIN authors a ON a.uid=p.author_uid "
        "LEFT JOIN rhythmics r ON r.id=p.rhythmic_id "
        "JOIN sources s ON s.id=p.src_id "
        "WHERE s.name=? ORDER BY p.id DESC LIMIT ? OFFSET ?",
        (USER_SRC, limit, offset)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["author"] = d.get("author") or None
        d["dynasty"], d["kind"], _ = S.parse_id(d["id"])
        out.append(d)
    return out


def get_current(con, pid: int) -> Optional[dict]:
    """取一条现有记录的文本字段，供 CLI edit 在未指定某字段时作默认值。"""
    row = con.execute(
        "SELECT p.title, a.name AS author, r.name AS rhythmic, p.tags "
        "FROM poems p LEFT JOIN authors a ON a.uid=p.author_uid "
        "LEFT JOIN rhythmics r ON r.id=p.rhythmic_id WHERE p.id=?",
        (pid,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["author"] = d.get("author") or ""
    d["rhythmic"] = d.get("rhythmic") or ""
    d["tags"] = [x for x in (d.get("tags") or "").split("\x1f") if x]
    return d


def count_user_added(con) -> int:
    row = con.execute(
        "SELECT COUNT(*) FROM poems p JOIN sources s ON s.id=p.src_id WHERE s.name=?",
        (USER_SRC,)).fetchone()
    return row[0] if row else 0


def list_mine(con, limit: int = 500, offset: int = 0) -> List[dict]:
    """自建 + 修订，合并列出（GUI "我的条目" 用这个，改过构建期数据的也在这里）。"""
    rows = con.execute(
        "SELECT p.id, p.title, a.name AS author, p.body, p.n_char, "
        "r.name AS rhythmic, p.tags, s.name AS src "
        "FROM poems p LEFT JOIN authors a ON a.uid=p.author_uid "
        "LEFT JOIN rhythmics r ON r.id=p.rhythmic_id "
        "JOIN sources s ON s.id=p.src_id "
        "WHERE s.name IN (?,?) ORDER BY p.id DESC LIMIT ? OFFSET ?",
        (USER_SRC, EDIT_SRC, limit, offset)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["author"] = d.get("author") or None
        d["dynasty"], d["kind"], _ = S.parse_id(d["id"])
        out.append(d)
    return out


def count_mine(con) -> int:
    row = con.execute(
        "SELECT COUNT(*) FROM poems p JOIN sources s ON s.id=p.src_id "
        "WHERE s.name IN (?,?)", (USER_SRC, EDIT_SRC)).fetchone()
    return row[0] if row else 0


def count_edited(con) -> int:
    row = con.execute(
        "SELECT COUNT(*) FROM poems p JOIN sources s ON s.id=p.src_id WHERE s.name=?",
        (EDIT_SRC,)).fetchone()
    return row[0] if row else 0


def export_edits(con, path: str) -> int:
    """
    把"修订过的构建期数据"导出成 JSONL，作为修正备份。
    why：这类数据源码（chinese_poetry_iterate 仓库）里没有修正版，重跑 build.py
    会从源码重生成、把修正覆盖掉。导出后至少保留了你的订正内容，可人工/脚本回填。
    """
    rows = con.execute(
        "SELECT p.id,p.title,a.name AS author,p.body,p.tags,s.name AS src "
        "FROM poems p LEFT JOIN authors a ON a.uid=p.author_uid "
        "JOIN sources s ON s.id=p.src_id WHERE s.name=?", (EDIT_SRC,)).fetchall()
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            d = dict(r)
            d["author"] = d.get("author") or None
            d["dynasty"], d["kind"], _ = S.parse_id(d["id"])
            d["tags"] = [x for x in (d.get("tags") or "").split("\x1f") if x]
            d["lines"] = (d.get("body") or "").split("\n")
            d.pop("body", None)
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
            n += 1
    return n


def stats(con) -> dict:
    n = con.execute("SELECT COUNT(*) FROM poems").fetchone()[0]
    a = con.execute("SELECT COUNT(*) FROM authors").fetchone()[0]
    return {"n_poems": n, "n_authors": a, "n_user_added": count_user_added(con)}


# ---------------------------------------------------------------- 核对状态
# 为什么单独一个 sidecar 库而不是在 poetry.db 里加张表：
#   核对标注是用户行为数据，和"从源码重建出来的诗词"生命周期完全不同。
#   放进 poetry.db 的话，重跑 build.py 重建主库会把标注一起清掉，等于"核对"白做。
#   独立文件 poetry-checks.db 永远不被 build 触碰，重建自动保留。
def open_checks(path: str = None, rw: bool = False):
    """打开核对库。rw=True 且文件不存在时会自动建表。只读且不存在则报错。"""
    if path is None:
        path = _here(CHECKS_DB)
    if not os.path.exists(path):
        if not rw:
            raise Rejected("核对库不存在：%s（首次标记核对时会自动创建）" % path)
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE IF NOT EXISTS checks("
                    "poem_id INTEGER PRIMARY KEY, verified INTEGER NOT NULL DEFAULT 0, "
                    "checked_at TEXT, note TEXT)")
        con.commit()
        con.close()
    if rw:
        con = sqlite3.connect(path)
    else:
        from urllib.request import pathname2url
        con = sqlite3.connect("file:" + pathname2url(os.path.abspath(path)) + "?mode=ro",
                              uri=True)
    con.row_factory = sqlite3.Row
    if rw:
        con.execute("CREATE TABLE IF NOT EXISTS checks("
                    "poem_id INTEGER PRIMARY KEY, verified INTEGER NOT NULL DEFAULT 0, "
                    "checked_at TEXT, note TEXT)")
        con.commit()
    return con


def set_checked(con, pid: int, verified: bool, note: str = "") -> None:
    """标记/取消某首诗的核对状态（UPSERT）。"""
    con.execute(
        "INSERT INTO checks(poem_id,verified,checked_at,note) VALUES(?,?,?,?) "
        "ON CONFLICT(poem_id) DO UPDATE SET verified=excluded.verified,"
        "checked_at=excluded.checked_at, note=excluded.note",
        (pid, 1 if verified else 0, time.strftime("%Y-%m-%dT%H:%M:%S"), note))
    con.commit()


def get_checked(con, pid: int) -> bool:
    row = con.execute("SELECT verified FROM checks WHERE poem_id=?", (pid,)).fetchone()
    return bool(row["verified"]) if row else False


def count_checked(con) -> int:
    row = con.execute("SELECT COUNT(*) FROM checks WHERE verified=1").fetchone()
    return row[0] if row else 0


def checked_batch(con, pids) -> dict:
    """一次取多首的核对状态，返回 {pid: bool}。pids 为空直接返回空 dict。"""
    out = {p: False for p in pids}
    if not pids:
        return out
    rows = con.execute(
        "SELECT poem_id,verified FROM checks WHERE poem_id IN (%s)"
        % ",".join("?" * len(pids)), pids).fetchall()
    for r in rows:
        out[r["poem_id"]] = bool(r["verified"])
    return out


# ---------------------------------------------------------------- 导出 / 导入
# why 需要这一对：build.py 是整库重建，跑一次就把自建条目全清了。
# 而自建条目是唯一没有别处副本的数据（源 JSON 里没有它），
# 所以必须在"任何人会重跑构建"这个前提成立之前提供迁移路径。
def export_user(con, path: str) -> int:
    """把自建条目导出成 JSONL。重建前跑一次，重建后再 import 回来。"""
    rows = list_user_added(con, limit=10 ** 7)
    for r in rows:
        r["tags"] = [x for x in (r.get("tags") or "").split("\x1f") if x]
        r["lines"] = (r.get("body") or "").split("\n")
        r.pop("body", None)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows)


def import_user(con, path: str, skip_exists: bool = True) -> tuple:
    """导入 export_user 产出的 JSONL。返回 (写入条数, 跳过条数)。"""
    if not os.path.exists(path):
        raise Rejected("文件不存在：%s" % path)
    written = skipped = 0
    with open(path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError as e:
                raise Rejected("第 %d 行不是合法 JSON：%s" % (ln, e))
            if skip_exists:
                dup = con.execute(
                    "SELECT 1 FROM poems WHERE title=? AND body=? LIMIT 1",
                    (d.get("title", ""), "\n".join(d.get("lines", [])))).fetchone()
                if dup:
                    skipped += 1
                    continue
            add_poem(con, dynasty=d.get("dynasty", "custom"),
                     kind=d.get("kind", "poem"), title=d.get("title", ""),
                     author=d.get("author") or "", lines=d.get("lines", []),
                     rhythmic=d.get("rhythmic") or "", tags=d.get("tags") or [])
            written += 1
    return written, skipped


def main():
    import argparse
    ap = argparse.ArgumentParser(description="命令行增删条目（GUI/HTTP 用的是同一层）")
    ap.add_argument("--db", default=_here("poetry.db"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="新增一条")
    p.add_argument("--dynasty", default="custom")
    p.add_argument("--kind", default="poem", choices=sorted(S.KIND))
    p.add_argument("--title", default="")
    p.add_argument("--author", default="")
    p.add_argument("--rhythmic", default="")
    p.add_argument("--tags", default="")
    p.add_argument("--body", required=True, help="正文，用 | 分行")

    p = sub.add_parser("list", help="列出自建条目")
    p.add_argument("--limit", type=int, default=50)
    sub.add_parser("stats")

    p = sub.add_parser("rm", help="删除一条（仅限自建）")
    p.add_argument("id", type=int)

    p = sub.add_parser("export", help="导出自建条目为 JSONL（重建前先跑）")
    p.add_argument("path")

    p = sub.add_parser("import", help="从 JSONL 导回自建条目（重建后跑）")
    p.add_argument("path")
    p.add_argument("--allow-dup", action="store_true", help="不去重，直接写入")

    p = sub.add_parser("edit", help="修订一条已存在的记录（保留 id，不改朝代/体裁）")
    p.add_argument("id", type=int)
    p.add_argument("--title", default=None)
    p.add_argument("--author", default=None)
    p.add_argument("--rhythmic", default=None)
    p.add_argument("--tags", default=None)
    p.add_argument("--body", default=None, help="正文，用 | 分行；不传则保留原文")

    p = sub.add_parser("export-edits", help="导出被修订过的构建期数据为 JSONL（修正备份）")
    p.add_argument("path")

    p = sub.add_parser("check", help="标记/查询诗词是否已核对（写入独立 sidecar 库）")
    p.add_argument("action", choices=["set", "get", "count"])
    p.add_argument("id", type=int, nargs="?")
    p.add_argument("val", type=int, nargs="?")

    args = ap.parse_args()
    con = open_rw(args.db)
    try:
        if args.cmd == "add":
            tags = [t for t in args.tags.split(",") if t.strip()]
            r = add_poem(con, dynasty=args.dynasty, kind=args.kind, title=args.title,
                         author=args.author, lines=args.body.split("|"),
                         rhythmic=args.rhythmic, tags=tags)
            print("已写入 id=%d  《%s》%s  字数=%d"
                  % (r["id"], r["title"], args.author or "佚名", r["n_char"]))
        elif args.cmd == "list":
            for r in list_user_added(con, args.limit):
                print("  id=%-12d [%s/%s] %s 《%s》"
                      % (r["id"], S.DYNASTY_LABEL.get(r["dynasty"]), r["kind"],
                         r["author"] or "佚名", r["title"]))
            print("  共 %d 条自建" % count_user_added(con))
        elif args.cmd == "stats":
            print(stats(con))
        elif args.cmd == "rm":
            delete_poem(con, args.id)
            print("已删除 id=%d" % args.id)
        elif args.cmd == "export":
            n = export_user(con, args.path)
            print("已导出 %d 条 -> %s" % (n, args.path))
        elif args.cmd == "edit":
            cur = get_current(con, args.id)
            if cur is None:
                print("拒绝：id=%d 不存在" % args.id, file=sys.stderr)
                sys.exit(2)
            r = update_poem(con, args.id,
                            title=args.title if args.title is not None else cur["title"],
                            author=args.author if args.author is not None else (cur["author"] or ""),
                            rhythmic=args.rhythmic if args.rhythmic is not None else cur["rhythmic"],
                            tags=args.tags.split(",") if args.tags is not None else cur["tags"],
                            lines=args.body.split("|") if args.body is not None else None)
            print("已修订 id=%d  《%s》（%d 字）" % (r["id"], r["title"], r["n_char"]))
        elif args.cmd == "export-edits":
            n = export_edits(con, args.path)
            print("已导出 %d 条修订 -> %s" % (n, args.path))
        elif args.cmd == "check":
            chk = open_checks(args.db, rw=True)
            if args.action == "set":
                if args.id is None or args.val is None:
                    print("用法: store.py check set <id> <0|1>", file=sys.stderr)
                    sys.exit(2)
                set_checked(chk, args.id, bool(args.val))
                print("已%s id=%d" % ("标记已核对" if args.val else "取消核对", args.id))
            elif args.action == "get":
                if args.id is None:
                    print("用法: store.py check get <id>", file=sys.stderr)
                    sys.exit(2)
                print("id=%d 核对状态: %s" % (args.id, "已核对" if get_checked(chk, args.id) else "未核对"))
            elif args.action == "count":
                print("已核对: %d 篇" % count_checked(chk))
        elif args.cmd == "import":
            w, s = import_user(con, args.path, skip_exists=not args.allow_dup)
            print("已写入 %d 条，跳过重复 %d 条" % (w, s))
    except Rejected as e:
        print("拒绝：%s" % e, file=sys.stderr)
        sys.exit(2)
    finally:
        con.close()


if __name__ == "__main__":
    main()
