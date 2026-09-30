# -*- coding: utf-8 -*-
"""
源数据注册表与读取器。

why 单独建这一层：源库有 16 个集合、正文字段名 4 种（paragraphs/content/para/spells）、
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


COLLECTIONS: List[Collection] = [
    Collection("tang-poem", "tang", "poem", r"全唐诗\poet.tang.*.json",
               tags="tags", notes="notes", has_id=True),
    Collection("song-poem", "song", "poem", r"全唐诗\poet.song.*.json",
               tags="tags", notes="notes", has_id=True),
    Collection("song-ci", "song", "ci", r"宋词\ci.song.*.json",
               rhythmic="rhythmic", tags="tags", has_id=False),
    Collection("yuan-qu", "yuan", "qu", r"元曲\yuanqu.json", has_id=False),
    Collection("shijing", "preqin", "poem", r"诗经\shijing.json",
               body="content", extra=["chapter", "section"]),
    Collection("chuci", "preqin", "poem", r"楚辞\chuci.json",
               body="content", extra=["section"]),
    Collection("lunyu", "preqin", "classic", r"论语\lunyu.json",
               title="chapter", author="", extra=["chapter"]),
    Collection("sishuwujing", "preqin", "classic", r"四书五经\*.json",
               title="chapter", author="", extra=["chapter"]),
    Collection("mengxue", "unknown", "classic", r"蒙学\*.json",
               body="content", tags="tags", extra=["abstract", "spells", "preface"],
               wrap_single=True),
    Collection("wudai-nantang", "wudai", "ci", r"五代诗词\nantang\poetrys.json",
               rhythmic="rhythmic", notes="notes"),
    Collection("wudai-huajianji", "wudai", "ci", r"五代诗词\huajianji\*.json",
               rhythmic="rhythmic", notes="notes"),
    Collection("youmengying", "qing", "prose", r"幽梦影\youmengying.json",
               body="content", notes="comment", author="", default_author="张潮"),
    Collection("caocao", "han", "poem", r"曹操诗集\caocao.json", author="",
               default_author="曹操"),
    Collection("shuimo-tangshi", "tang", "poem", r"水墨唐诗\shuimotangshi.json",
               extra=["prologue"]),
    Collection("nalanxingde", "qing", "poem", r"纳兰性德\纳兰性德诗集.json",
               body="para"),
    # 御定全唐詩：与全唐诗内容高度重叠（标题交集 69.8%）但分段方式不同（十首合一 vs 逐首），
    # 作为"异文版本"单独成包，默认不进主库，避免检索结果重复。
    Collection("yuding-tangshi", "tang", "poem", r"御定全唐詩\json\*.json",
               notes="notes", extra=["volume", "biography", "no#"], optional=True),
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
    """作者库。名字同样做简体归一，佚名类直接丢掉——主库里 author_id=0 即佚名，
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
