# -*- coding: utf-8 -*-
"""
源数据注册表与读取器。

why 单独建这一层：源库有 17 个集合、正文字段名 4 种（paragraphs/content/para/spells）、
作者字段名 2 种、有无 id 不一致。若把兼容逻辑散进构建流程，每加一个集合就要改主流程。
这里把所有"脏"收敛成一张表，主流程只消费统一记录。

重要：分片文件按文件名里的数字 N 排序，不能按字典序。
字典序会得到 0,1000,10000,100000,... 与 rank/ 分片错位，热度数据会静默挂到错误的诗上。
"""

from __future__ import annotations
import os, re, json, glob
from dataclasses import dataclass, field
from typing import List, Dict, Iterator, Callable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_LEGACY_ROOT = r"E:\chinese_poetry_iterate"

# 源库位置自适应，不再写死盘符。why：工程会跟着源库一起搬（比如放到源库根目录下
# 用 PyCharm 打开），写死绝对路径意味着每换一台机器就要改代码，而这类改动最容易
# 在协作时漏掉。优先级：环境变量 POETRY_SRC > 工程同级/工程内 > 旧硬编码路径。
# 判定依据是"这个目录里有没有 strains/ 或 rank/ 子目录"，而不是只看目录存在。
def _detect_root() -> str:
    env = os.environ.get("POETRY_SRC")
    if env:
        return env
    for cand in (os.path.dirname(_HERE), _HERE, _LEGACY_ROOT):
        for probe in ("strains", "rank"):
            if os.path.isdir(os.path.join(cand, probe)):
                return cand
    return _LEGACY_ROOT


ROOT = _detect_root()

# 外接数据目录：源库里没有的集合（歌赋 / 清代诗词 / 毛选…）放在这里，
# 用「统一摄入格式」写成一个 JSON 文件，构建时自动发现、无需改主流程。
# why 做成目录自动发现而不是硬编码进 COLLECTIONS：后续每加一个集就要改一次
# registry，等于把扩展成本转嫁给维护者；目录发现让"加数据"和"改代码"解耦。
EXTERNAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "external")

# ---------------------------------------------------------------- 分片排序
_NUM = re.compile(r"(\d+)")


def natural_key(path: str) -> tuple:
    """按路径中的数字排序：poet.song.2000.json < poet.song.10000.json"""
    parts = []
    for seg in os.path.basename(path).split("."):
        if seg.isdigit():
            parts.append((1, int(seg), ""))
        else:
            parts.append((0, 0, seg))
    return tuple(parts)


def shard_no(path: str) -> int:
    """从 poet.song.12000.json / poet.song.rank.12000.json 里取出 12000。"""
    nums = _NUM.findall(os.path.basename(path))
    # 取最后一个数字段，避开 rank 里可能出现的多数字
    return int(nums[-1]) if nums else 0


def glob_shards(pattern: str, exclude_error: bool = True) -> List[str]:
    files = glob.glob(os.path.join(ROOT, pattern))
    if exclude_error:
        files = [f for f in files if os.sep + "error" + os.sep not in f]
    return sorted(files, key=natural_key)


def load_json(path: str):
    """单文件读取。失败时抛异常由调用方决定跳过还是中止——静默跳过会丢数据。"""
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


# ---------------------------------------------------------------- 集合定义
@dataclass
class Collection:
    name: str
    dynasty: str
    kind: str
    pattern: str                      # 相对 ROOT 的 glob
    body: str = "paragraphs"          # 正文字段名
    title: str = "title"
    author: str = "author"
    rhythmic: str = ""
    tags: str = ""
    notes: str = ""
    extra: List[str] = field(default_factory=list)   # 原样搬进 extra 的字段
    has_id: bool = False
    wrap_single: bool = False         # 文件里是单个对象而非数组
    optional: bool = False            # 可选包：默认不进主产物
    external: bool = False            # 外接集合：数据来自 external/ 目录，非源库
    # 源数据整集都没有作者字段时的补录值。why：不补的话这些作品作者全是 NULL，
    # 既没法按作者检索，也和"佚名=NULL"的语义混在一起——真佚名和"作者没录入"是两回事。
    # 只对"集合名本身即作者"或作者毫无争议的集合补录，其余一律留 NULL，不猜。
    default_author: str = ""
    label: str = ""                   # 中文名，仅用于展示
    # 整集的体裁 id（见 schema.GENRES：1 诗 2 词 3 曲 4 赋 5 骚 6 文 7 新诗 8 联）。
    # 0 = 不指定，由 schema.KIND_GENRE 按 kind 推（全唐诗=诗、宋词=词、蒙学=文…）。
    # 只有「kind 表达不出真正体裁」的集合才需要显式写：楚辞（kind=poem 但其实是骚）、
    # 纳兰词（kind=poem 但整本是词）。八集已逐条标注 genreId 的集合不受这里影响——
    # 记录里的 genreId 优先级更高，见 build.build_poems。
    # why 放在集合级而不是逐条判：这些文件本身是单一文体的选本，整集同 ID 就是
    # 「内容属于同一类就统一加同一个 genreId」，不必（也不该）回头改源文件。
    genre: int = 0


COLLECTIONS: List[Collection] = [
    Collection("tang-poem", "tang", "poem", r"全唐诗\poet.tang.*.json",
               tags="tags", notes="notes", has_id=True),
    Collection("song-poem", "song", "poem", r"全唐诗\poet.song.*.json",
               tags="tags", notes="notes", has_id=True),
    Collection("song-ci", "song", "ci", r"宋词\ci.song.*.json",
               rhythmic="rhythmic", tags="tags", has_id=False),
    Collection("shijing", "preqin", "poem", r"诗经\shijing.json",
               body="content", extra=["chapter", "section"]),
    Collection("chuci", "preqin", "poem", r"楚辞\chuci.json",
               body="content", extra=["section"], genre=5),
    Collection("lunyu", "preqin", "classic", r"论语\lunyu.json",
               title="chapter", author="", extra=["chapter"]),
    Collection("sishuwujing", "preqin", "classic", r"四书五经\*.json",
               title="chapter", author="", extra=["chapter"]),
    # 蒙学里既有「单篇 dict」（三字经/千字文…）又有「篇目列表」（古文观止 222 篇），
    # 故不能开 wrap_single——它会把 list 再包一层变成 [[...]]，整份数据被静默丢弃。
    # iter_collection 里的 `isinstance(data, dict)` 已能处理单篇 dict，无需额外开关。
    Collection("mengxue", "unknown", "classic", r"蒙学\*.json",
               body="content", tags="tags", extra=["abstract", "spells", "preface"]),
    Collection("wudai-nantang", "wudai", "ci", r"五代诗词\nantang\poetrys.json",
               rhythmic="rhythmic", notes="notes"),
    Collection("wudai-huajianji", "wudai", "ci", r"五代诗词\huajianji\*.json",
               rhythmic="rhythmic", notes="notes"),
    Collection("youmengying", "qing", "prose", r"幽梦影\youmengying.json",
               body="content", notes="comment", author="", default_author="张潮"),
    # 曹操诗集（26 首）已于 2026-10-26 并入汉（曹操归汉，tags=曹操诗集），目录已删除。
    # 这批诗的 dynasty 统一为 han，作者行复用汉 authors 里的曹操（id 9562cf46…）。
    # 水墨唐诗（176 首诵读选本）已于 2026-10-19 并入全唐诗：176 首逐篇命中原诗，
    # 原记录不保留，只在原诗上追加 tags=水墨唐诗 与 notes=白话译文；目录已删除。
    # 因此这里不再有 shuimo-tangshi 集合，也不再重复读一份 tang/poem。
    Collection("nalanxingde", "qing", "poem", r"纳兰性德\纳兰性德诗集.json",
               body="para", genre=2),
    # 御定全唐詩：已逐篇与全唐诗去重（仅保留 2,675 首独有篇目）并繁转简，
    # 分片命名与 全唐诗 同构（poet.yuding.NNNN.json，每片 1000 首，NNNN 为累计偏移）。
    # 仍保持 optional：是否入主库由 --with-optional 决定，避免检索结果混入重复版本。
    Collection("yuding-tangshi", "tang", "poem", r"御定全唐詩\poet.yuding.*.json",
               tags="tags", notes="notes", optional=True),
    # 近现代诗词：2026-10 自 shicihui.com 近现代分类逐页（494 页）采集，
    # 11848 首 / 101 位作者，分片命名与 全唐诗 同构（poet.jinxiandai.NNNN.json，
    # 每片 1000 首），每条带 UUID 的 id，故 has_id=True，可直接复用全唐诗那条
    # 「按 id 回溯源库」的路径。dynasty=modern 与 kind=poem 都已是 schema 里的既有枚举
    # （modern=9 就是为该集预留的），不需要改编码表。
    # why 追加在列表**末尾**而不是插进同事之间：poems.id 的低 24 位是集合内序号，
    # 插到中间会让其后每个集合的 seq 整体位移、id 全变；追加则一个既有 id 都不动。
    # 体裁由记录里的 genreId 直接读到 poems.genre_id（v3.5 起；字典表 v3.6 起叫 genre），
    # 不再走 extra：
    # extra 是给 prologue/biography 这类长文本用的，体裁有自己的一列与 bundle 的 y 键。
    Collection("jinxiandai", "modern", "poem", r"近现代\poet.jinxiandai.*.json",
               has_id=True),
    # 2026-10 补全的缺失朝代（古文岛分站采集，stage1/2/3 产出）。
    # 命名 <code>-new 以区别于既有集合；追加在列表**末尾**，避免 poems.id 的
    # 集合内 seq 位移（见上方 jinxiandai 注释）。复用 poet.<code>.0.json
    # （body=paragraphs / title / author / id 均为既有命名约定），has_id=True
    # 以便按 id 回溯源库。朝代编码 weijin/nanbeichao/sui/jin 已在 schema.py 新增。
    # 这八集已逐条标注体裁（记录里的 genreId，登记表 genres.json）：构建时读进
    # poems.genre_id，源文件不必再改；也不参与 make_id / make_uid，故 id 全不变。
    Collection("han-new", "han", "poem", r"汉\poet.han.*.json",
               tags="tags", has_id=True),
    Collection("weijin-new", "weijin", "poem", r"魏晋\poet.weijin.*.json",
               tags="tags", has_id=True),
    Collection("nanbeichao-new", "nanbeichao", "poem", r"南北朝\poet.nanbeichao.*.json",
               has_id=True),
    Collection("sui-new", "sui", "poem", r"隋\poet.sui.*.json", has_id=True),
    Collection("jin-new", "jin", "poem", r"金\poet.jin.*.json", has_id=True),
    Collection("yuan-new", "yuan", "poem", r"元诗\poet.yuanshi.*.json", has_id=True),
    Collection("ming-new", "ming", "poem", r"明\poet.ming.*.json", has_id=True),
    Collection("qing-new", "qing", "poem", r"清诗\poet.qingshi.*.json", has_id=True),
    # 2026-10-26 朝代对齐：自 shiwudi.com 采集的三国/晋（网站拆分「魏晋」而来，
    # 另有「西夏」0 篇未收录；网站「辽」6 篇系误标北宋诗，已弃用）。命名沿用
    # <code>-new，追加在末尾不位移既有 id。
    # has_id=True：记录的 id 是本地确定性 UUID（uuid5，见 _scrape/gen_dynasty_json.py），
    # 不是源站数字 id——因为 shiwudi 的 poetry 只有数字 id，本地补全朝代惯例用 UUID。
    # 三国（2026-10-26 上旬）与晋（2026-10-26 傍晚）均已并入魏晋：
    #   三国 tags=["三国"]、晋 tags=["晋"]，dynasty 统一 weijin，源目录已删除，
    #   故 sango-new / jinchao-new 两个集合在此移除（原 jinchao 编码保留为死编码）。
]

# ---------------------------------------------------------------- 外接集合
# 统一摄入格式（external/<slug>.json）：
# {
#   "collection": "qing-poetry",      # 集合名，落进 sources 字典
#   "label": "清代诗词",               # 中文名，仅展示用
#   "dynasty": "qing",                # 必须是 schema.DYNASTY 里的键
#   "kind": "poem",                   # 必须是 schema.KIND 里的键
#   "default_author": "",             # 整集缺作者时的补录值，猜不出来就留空
#   "enabled": true,                  # false 则跳过，方便临时停用而不删文件
#   "note": "",
#   "items": [
#     {"title":"己亥杂诗","author":"龚自珍","lines":["浩荡离愁白日斜","吟鞭东指即天涯"],
#      "rhythmic":"","tags":["七言绝句"],"notes":[]}
#   ]
# }
# 朝代/体裁必须是已知枚举：写错会让 id 高位编码出未知值，反解时 silently 变成别的集合。
# 所以这里直接抛错，不做任何"猜一个默认值"的兜底。
REQUIRED_EXTERNAL_KEYS = ("collection", "dynasty", "kind", "items")


def load_external_spec(path: str) -> dict:
    with open(path, "r", encoding="utf-8-sig") as f:
        spec = json.load(f)
    if not isinstance(spec, dict):
        raise ValueError("%s：外接文件顶层必须是对象" % path)
    missing = [k for k in REQUIRED_EXTERNAL_KEYS if k not in spec]
    if missing:
        raise ValueError("%s：缺少必填字段 %s" % (path, "/".join(missing)))
    return spec


def validate_enum(spec: dict, path: str):
    import schema as S
    if spec["dynasty"] not in S.DYNASTY:
        raise ValueError("%s：未知 dynasty=%r，可选 %s"
                         % (path, spec["dynasty"], "/".join(sorted(S.DYNASTY))))
    if spec["kind"] not in S.KIND:
        raise ValueError("%s：未知 kind=%r，可选 %s"
                         % (path, spec["kind"], "/".join(sorted(S.KIND))))


def discover_external(dirpath: str = "") -> List[Collection]:
    """扫描 external/*.json，每个文件 = 一个外接集合。

    文件不存在、格式错误都要显式报错而不是静默跳过：外接数据是用户手写的，
    字段名拼错（lines 写成 line）是最常见的问题，静默跳过会表现为"数据少了"
    而不是"配置错了"，排查成本极高。
    """
    d = dirpath or EXTERNAL_DIR
    if not os.path.isdir(d):
        return []
    out: List[Collection] = []
    for path in sorted(glob.glob(os.path.join(d, "*.json"))):
        spec = load_external_spec(path)
        if not spec.get("enabled", True):
            print("  外接 %s 已停用（enabled=false），跳过" % os.path.basename(path))
            continue
        validate_enum(spec, path)
        out.append(Collection(
            name=spec["collection"],
            dynasty=spec["dynasty"],
            kind=spec["kind"],
            pattern=path,
            body="lines",
            title="title",
            author="author",
            rhythmic="rhythmic",
            tags="tags",
            notes="notes",
            # extra 取记录级可选字段；没有就自然为空，不影响主流程
            extra=["chapter", "section", "volume", "year"],
            external=True,
            default_author=spec.get("default_author", ""),
            label=spec.get("label", spec["collection"]),
        ))
    return out


def iter_external(coll: Collection) -> Iterator[tuple[str, dict]]:
    spec = load_external_spec(coll.pattern)
    for i, rec in enumerate(spec.get("items") or []):
        if not isinstance(rec, dict):
            raise ValueError("%s：items[%d] 不是对象" % (coll.pattern, i))
        yield coll.pattern, rec


# ---------------------------------------------------------------- 作者库
@dataclass
class AuthorSource:
    name: str
    dynasty: str
    pattern: str
    name_key: str = "name"
    desc_key: str = "desc"


AUTHOR_SOURCES: List[AuthorSource] = [
    AuthorSource("authors.tang", "tang", r"全唐诗\authors.tang.json"),
    AuthorSource("authors.song", "song", r"全唐诗\authors.song.json"),
    AuthorSource("songci.authors", "song", r"宋词\author.song.json", desc_key="description"),
    AuthorSource("nantang.authors", "wudai", r"五代诗词\nantang\authors.json"),
    # 阶段 2 新建：这三个语料此前同样没有作者文件，其作者全部退化为佚名。
    # 屈原、东方朔、贾谊、刘向等楚辞名家，以及纳兰性德、周兴嗣、朱柏庐等，
    # 在此之前 load_authors() 里查不到任何记录。字段结构同上（desc/name/id）。
    #
    # 第二参数是 **dynasty，不是来源 id**（2026-10-20 修正）：此前误填成集合名
    # （chuci / nalan / mengxue），这 15 位作者的 authors.dynasty 因此落在
    # schema.DYNASTY 编码之外——库里出现 id>>27 反解不出来的非法朝代值，连带
    # make_author_uid 的派生键也带上垃圾串。三个值一律对齐所属 Collection：
    # 楚辞=preqin、纳兰性德=qing、蒙学=unknown（蒙学一集本身即 unknown，作者跨
    # 南朝梁/明/清三代，不臆断到具体朝代）。改这里会让这 15 人的 author_uid 变化，
    # 重建后需重跑 author_heat.py --apply 把热度分按新 uid 写回。
    AuthorSource("chuci.authors", "preqin", r"楚辞\authors.json"),
    AuthorSource("nalan.authors", "qing", r"纳兰性德\authors.json"),
    AuthorSource("mengxue.authors", "unknown", r"蒙学\authors.json"),
    # 水墨唐诗的 authors.json 只有一条「韩竑」（即韩翃的讹写），其《寒食》已并入
    # 韩翃名下，作者小传由 全唐诗/authors.tang.json 提供，无需单独作者源。
    # 御定全唐詩（清康熙年奉敕编纂，900 卷）同样无作者文件，
    # 正文已改为与全唐诗同构的分片（poet.yuding.NNNN.json）。字段结构同上。
    AuthorSource("yuding.authors", "tang", r"御定全唐詩\authors.json"),
    # 近现代作者小传：与上面的 jinxiandai 一集同源（shicihui.com 采集，101 位）。
    # 字段结构与 全唐诗\authors.tang.json 一致（desc/name/id），dynasty 必须与集合
    # 的 modern 完全相同，否则 load_authors() 出来的名字挂不到那批作品上。
    AuthorSource("jinxiandai.authors", "modern", r"近现代\authors.jinxiandai.json"),
    # 三国 authors.sango.json 已于 2026-10-26 并入魏晋 authors.weijin.json
    # （曹叡/应璩 新增，曹植/嵇康/阮籍/曹丕 复用魏晋既有条目；曹操归汉，见 han.authors）。
    # 晋 authors.jinchao.json 同日并入：新增陆云/曹摅/卢谌/孙楚/袁宏/杨济 6 位
    # （id 沿用晋源文件），并给全部作者补 dynasty=实际年代（西晋/东晋/曹魏…）。
    # 两个源目录已删除，故 sango.authors / jinchao.authors 在此移除。
    # 补全朝代作者小传（2026-10 自古文岛分站采集，与下方 -new 集合同源）。
    # 字段结构（desc/name/id）与 全唐诗\authors.tang.json 一致；dynasty 必须与
    # 所属 Collection 相同，否则 load_authors() 出来的名字挂不到那批作品上。
    # 此前遗漏：这八个 authors.*.json 一直没进 AUTHOR_SOURCES，作者小传（desc 有
    # 完整传记）在构建时被 merge_poem_authors 以空 desc 补齐，等于白采集。
    AuthorSource("han.authors", "han", r"汉\authors.han.json"),
    AuthorSource("weijin.authors", "weijin", r"魏晋\authors.weijin.json"),
    AuthorSource("nanbeichao.authors", "nanbeichao", r"南北朝\authors.nanbeichao.json"),
    AuthorSource("sui.authors", "sui", r"隋\authors.sui.json"),
    AuthorSource("jin.authors", "jin", r"金\authors.jin.json"),
    AuthorSource("yuan.authors", "yuan", r"元诗\authors.yuanshi.json"),
    AuthorSource("ming.authors", "ming", r"明\authors.ming.json"),
    AuthorSource("qing.authors", "qing", r"清诗\authors.qingshi.json"),
]

# ---------------------------------------------------------------- 关联数据
# 平仄：按 UUID 关联（strains/json/poet.{tang,song}.N.json ↔ 全唐诗/poet.{tang,song}.N.json）
STRAIN_MAP = {
    "tang-poem": r"strains\json\poet.tang.*.json",
    "song-poem": r"strains\json\poet.song.*.json",
}
# 热度：无 id，靠 (author,title) 字符串 + 分片序号双重校验后按位对齐
RANK_MAP = {
    "tang-poem": r"rank\poet\poet.tang.rank.*.json",
    "song-poem": r"rank\poet\poet.song.rank.*.json",
    "song-ci": r"rank\ci\ci.song.rank.*.json",
}


def iter_collection(coll: Collection) -> Iterator[tuple[str, dict]]:
    """产出 (来源文件路径, 原始记录)。单文件多记录时路径重复出现，用于定位脏数据。"""
    if coll.external:
        yield from iter_external(coll)
        return
    files = glob_shards(coll.pattern)
    if not files:
        return
    for path in files:
        # 作者元数据（{id,name,desc}）不是作品，蒙学/authors.json 即此类。
        # 不跳过也不会出错（body 为空 → 被空正文丢弃），但会污染「空正文丢弃」这个脏数据指标。
        if os.path.basename(path) == "authors.json":
            continue
        data = load_json(path)
        if coll.wrap_single or isinstance(data, dict):
            data = [data]
        for rec in data:
            if isinstance(rec, dict):
                yield path, rec


def _flatten(x, out: List[str]):
    """递归取文本。蒙学的 content 是嵌套的 [{title, content:[...]}]，
    直接 str() 会得到 "{'chapter':'总叙',...}" 这种垃圾，必须展开。"""
    if isinstance(x, str):
        t = x.strip()
        if t:
            out.append(t)
        return
    if isinstance(x, (list, tuple)):
        for i in x:
            _flatten(i, out)
        return
    if isinstance(x, dict):
        for k in ("title", "chapter", "section"):
            v = x.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
        for k in ("paragraphs", "content", "para", "text"):
            if k in x:
                _flatten(x[k], out)


def get_body(rec: dict, coll: "Collection") -> List[str]:
    """正文有 4 种叫法；spells 只在千字文里出现且是拼音，不属于正文，这里不取。"""
    v = rec.get(coll.body)
    if v is None and coll.body != "paragraphs":
        v = rec.get("paragraphs")
    if v is None:
        v = rec.get("content")
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        if all(isinstance(x, str) for x in v):
            return [x for x in v]
        out: List[str] = []
        _flatten(v, out)
        return out
    if isinstance(v, dict):
        out = []
        _flatten(v, out)
        return out
    return []


def pick_title(rec: dict, coll: "Collection", body: List[str]) -> str:
    """宋词源数据只有词牌没有标题，直接用会显示成《》。
    标题缺失时按 词牌 -> 首行前 12 字 兜底，保证任何记录都有可读标题。"""
    t = (rec.get(coll.title) or "").strip() if isinstance(rec.get(coll.title), str) else ""
    if not t and coll.rhythmic:
        t = (rec.get(coll.rhythmic) or "").strip()
    if not t and body:
        t = body[0][:12]
    return t


def load_authors() -> List[dict]:
    """作者库。名字同样做简体归一，佚名类直接丢掉——主库里 author_uid='' 即佚名，
    不需要为"无名氏/無名氏/不详"这些占位名建条目。"""
    import schema as S
    out = []
    for src in AUTHOR_SOURCES:
        for path in glob_shards(src.pattern):
            for rec in load_json(path):
                nm = S.norm_author(rec.get(src.name_key) or "")
                if not nm:
                    continue
                out.append({
                    "name": nm,
                    "dynasty": src.dynasty,
                    "desc": S.norm_text(rec.get(src.desc_key) or ""),
                    "src": src.name,
                })
    return out
