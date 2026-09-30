# -*- coding: utf-8 -*-
"""
统一数据模型定义。

设计要点（why）：
1. id 用 int32 而非 UUID：原库 311,855 首诗各带一个 36 字符 UUID，纯占 10.71 MB
   且无顺序性，无法按范围定位集合。int32 高位编码 dynasty/kind，低位为序号，
   使得 "给定 id 就知道它属于哪个集合" 无需额外索引。
2. lines 统一字段名：源库里正文有 paragraphs / content / para 三种叫法，
   平仄另存一整套并行文件，全部收敛到一个字段名。
3. strains 用 4bit 打包：平仄字符集实测只有 7 个符号（仄/平/，/。/○/？/通），
   与正文逐字 1:1 对应，4bit 足够，实测压缩 83.3%，叠 zstd 后 95%+。
"""

from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

# 简体归一是本版本的硬要求：宁可构建期直接失败，也不能静默产出混着繁体的数据
# （一旦混入繁体，"乡愁" 搜 "鄉愁" 的老问题就会复活，且极难在下游定位）。
try:
    from zhconv import convert as _zh_convert
except ImportError:
    _zh_convert = None

# ---------------------------------------------------------------- 版本
# 版本必须随「落库形态」变，而不只是随代码变。消费方要靠它判断库里有没有某张表：
#   1.0  初版：单库含 poem_strains，正文保留繁体 + 另存 body_s 简体副本
#   2.0  简体归一、无空格、佚名=NULL、字典化；poem_strains 仍在主库
#   3.0  平仄拆成独立可选包（主库不再有 poem_strains 表），新增外接数据/自建号段
#        判断方法：主库 meta.schema_version >= 3.0 即代表平仄需从 poetry-strains.db 取
#   3.1  poems 表新增 uid 列：内容派生的稳定 uuid，供 extras 等外部数据关联
SCHEMA_VERSION = "3.1"

# ---------------------------------------------------------------- 编码表
# 位布局: (dynasty << 27) | (kind << 24) | seq  -> 最大 2^31-1，可存进 int32
DYN_BITS, KIND_BITS, SEQ_BITS = 4, 3, 24
SEQ_MAX = (1 << SEQ_BITS) - 1  # 16,777,215

DYNASTY = {
    "unknown": 0,
    "preqin": 1,   # 先秦（诗经、楚辞、四书五经）
    "han": 2,      # 汉（曹操诗集）
    "wudai": 3,    # 五代（花间集、南唐）
    "tang": 4,     # 唐
    "song": 5,     # 宋
    "yuan": 6,     # 元
    "ming": 7,     # 明
    "qing": 8,     # 清（纳兰性德、幽梦影、外接的清代诗词）
    "modern": 9,   # 近现代（毛选等）
    # 15 固定保留给「自建」：GUI 录入 / API 新增的条目写在这里。
    # why 用最高位而不是另起一张表：新增条目与既有条目共用同一条查询路径，
    # 不需要在 search() 里 union 两张表，也不会和构建期生成的 id 撞号。
    "custom": 15,
}
DYNASTY_NAME = {v: k for k, v in DYNASTY.items()}

KIND = {
    "poem": 0,     # 诗
    "ci": 1,       # 词
    "qu": 2,       # 曲
    "prose": 3,    # 散文 / 文
    "classic": 4,  # 经 / 蒙学
    "fu": 5,       # 赋 / 歌赋（洛神赋、赤壁赋…）
    "article": 6,  # 论著 / 文章（毛选等非韵文）
}
KIND_NAME = {v: k for k, v in KIND.items()}

DYNASTY_LABEL = {
    "preqin": "先秦", "han": "汉", "wudai": "五代", "tang": "唐",
    "song": "宋", "yuan": "元", "ming": "明", "qing": "清",
    "modern": "近现代", "unknown": "未详", "custom": "自建",
}

KIND_LABEL = {
    "poem": "诗", "ci": "词", "qu": "曲", "prose": "文",
    "classic": "经", "fu": "赋", "article": "论著",
}

# 自建号段的 seq 起点：与构建期产物（从 0 开始）错开 1000 万，
# 避免同一 (dynasty,kind) 下手工录入与批量导入互相覆盖。
CUSTOM_SEQ_BASE = 10_000_000


def make_id(dynasty: str, kind: str, seq: int) -> int:
    """生成全局 id。seq 超限时抛错而不是静默回绕——静默回绕会造出重复 id。"""
    if seq > SEQ_MAX:
        raise ValueError("seq %d 超出 %d（%s/%s 分片过大）" % (seq, SEQ_MAX, dynasty, kind))
    return (DYNASTY[dynasty] << (KIND_BITS + SEQ_BITS)) | (KIND[kind] << SEQ_BITS) | seq


def parse_id(pid: int) -> tuple[str, str, int]:
    return (
        DYNASTY_NAME[(pid >> (KIND_BITS + SEQ_BITS)) & 0xF],
        KIND_NAME[(pid >> SEQ_BITS) & 0x7],
        pid & SEQ_MAX,
    )


# uid 用的是固定命名空间，换掉会让全库 uid 集体失效（等于把 extras 全打散），别改。
POEM_NS = uuid.UUID("6f5b1c0e-9a3d-4f86-8c21-7d4e2a9b1f30")


def make_uid(dynasty: str, author: str, title: str, body: str) -> str:
    """由内容派生的稳定 uid（uuid5：确定性哈希，不是随机数，重建 N 次结果一致）。

    why 需要它：int32 的 id 高位编码 dynasty/kind、低位是**序号**，
    dedup 或集合顺序一变，序号整体位移，id 就跟着变 —— 挂在主库外面的
    extras（译文/注释/赏析/配图）是按 id 关联的，一重建就全错位。
    uid 只由内容决定，所以对「重建 / 重排序 / 增删别的诗」完全免疫。

    字段组合是实测选出来的（345,358 首全量跑过）：
      朝代+作者+标题+正文        冲突 0 组            ← 采用
      朝代+标题+正文(去作者)     冲突 468 组 / 1094 行
      标题+正文                  冲突 801 组 / 1764 行
      仅正文                     冲突 2387 组 / 5129 行
    去掉作者虽能换来「改作者不改 uid」，但会造出近千条撞号，不可用。

    代价（必须说清楚）：uid 是内容哈希，所以**改动这一首自身的作者/标题/正文，
    uid 就会变**。它对别人的增删免疫，对自己被改写不免疫。改完内容后需要把
    对应 extras 记录的 uid 刷新一次（见 remap_extras_uid.py）。
    """
    return str(uuid.uuid5(POEM_NS, "\x01".join(
        x or "" for x in (dynasty, author, title, body))))


# ---------------------------------------------------------------- 平仄打包
# 实测频次（唐诗全量 365 万符号）: 仄 548858 / 平 509299 / ，102176 / 。101541 /
#                                 ○ 64678 / ？1395 / 通 3
STRAIN_ALPHA = {"仄": 0, "平": 1, "，": 2, "。": 3, "○": 4, "？": 5, "通": 6}
STRAIN_UNK = 7
STRAIN_ALPHA_INV = {v: k for k, v in STRAIN_ALPHA.items()}
STRAIN_ALPHA_INV[STRAIN_UNK] = "?"


def pack_strains(s: str) -> bytes:
    """逐字 4bit 打包；奇数长度补一个 0 nibble（解包时按原文本长度截断）。"""
    vals = [STRAIN_ALPHA.get(c, STRAIN_UNK) for c in s]
    if len(vals) % 2:
        vals.append(0)
    out = bytearray(len(vals) // 2)
    for i in range(0, len(vals), 2):
        out[i // 2] = (vals[i] << 4) | vals[i + 1]
    return bytes(out)


def unpack_strains(b: bytes, length: int) -> str:
    """length = 原平仄字符串的符号数，用于丢弃补齐的那个 nibble。"""
    out = []
    for byte in b:
        out.append(STRAIN_ALPHA_INV[byte >> 4])
        out.append(STRAIN_ALPHA_INV[byte & 0xF])
    return "".join(out[:length])


# ---------------------------------------------------------------- 文本归一
# 全库统一转成简体并去掉空格。转简体后原文即检索文本，
# 不再需要额外存一份"简体副本"——这是本项目最大的一次瘦身（省掉一整份正文）。
#
# 实测：唐诗 27.82%、宋诗 26.65% 字符为繁体，宋词/元曲已是简体；
# 空格只出现在标题（如"帝京篇十首 一"），正文抽样 79,607 条空格数为 0。

_SPACES = (" ", "\u3000", "\t", "\xa0", "\u2007", "\u202f")


def norm_text(s: str) -> str:
    """
    简体归一 + 去空格。zhconv 不可用时抛错（不静默降级）。

    why 要跑到不动点（最多 3 轮）：zhconv 走的是「词组级最长匹配」，不是逐字映射。
    实测 "積善忻餘慶" 第一轮命中的是词组规则 → "积善忻馀庆"（馀 是简体异体，不是繁体），
    再转一轮才收敛到 "积善忻余庆"。只转一次会在库里留下 0.14% 的异体字，
    既不算繁体也不算标准简体，检索时 "余庆" 与 "馀庆" 会割裂。
    不动点归一保证了不变量 convert(x) == x，校验脚本可以直接拿这条当断言。
    """
    if not s:
        return ""
    if _zh_convert is None:
        raise RuntimeError("缺少依赖：pip install zhconv==1.4.3（简体归一为硬要求）")
    prev = s
    for _ in range(3):
        cur = _zh_convert(prev, "zh-cn")
        if cur == prev:
            break
        prev = cur
    for ch in _SPACES:
        if ch in prev:
            prev = prev.replace(ch, "")
    return prev


def norm_lines(lines: List[str]) -> List[str]:
    out = []
    for l in lines:
        t = norm_text(l)
        if t:
            out.append(t)
    return out


# 佚名类作者。源库里实测有 111 种写法（无名氏 3502 / 不詳 886 / 無名氏 875 /
# 无名氏《张协状元》305 / 佚名 261 …），统一转简体后再判定，繁简都能覆盖。
# 注意：王氏、花蕊夫人徐氏 这类"某氏"是真名（以姓氏指代），不在此列，不能误杀。
# "南唐失名僧" 这类是「朝代 + 失名 + 身份」，只能按子串判定，不能只做精确匹配。
_ANON_EXACT = {"不详", "佚", "匿名", "无名", "无名的", "失名", "阙名", "闕名"}
_ANON_TOKENS = ("佚名", "无名", "失名", "阙名")


def norm_author(a: str) -> str:
    """佚名类返回空串（落库为 NULL）；其余返回简体去空格后的名字。"""
    a = norm_text(a).strip()
    if not a:
        return ""
    if a in _ANON_EXACT:
        return ""
    for t in _ANON_TOKENS:
        if t in a:
            return ""
    return a


# ---------------------------------------------------------------- 热度量化
# rank 源数据 baidu/bing/google/so360 值域 0~16,100,000，中位数 90。
# 直接存 4 个 int 要 16 字节/条；热度只用于排序，1 个 uint8 足够。
def quantize_score(values: List[int]) -> int:
    v = max(values) if values else 0
    if v <= 0:
        return 0
    import math
    # log10 映射到 0..255，16.1e6 为实测最大值
    s = int(255.0 * math.log10(1 + v) / math.log10(1 + 16_100_000))
    return max(1, min(255, s))


# ---------------------------------------------------------------- 统一记录
# 全部文本在建库时已归一为「简体 + 无空格」，因此：
#   - body 既是展示文本也是检索文本，不再需要第二份副本
#   - 朝代 / 体裁 不落库：id 高位已编码，用 parse_id() 反解即可
#   - 作者 / 标题 / 词牌 / 来源 用字典 id，避免 34 万行里重复堆同样的字符串
@dataclass
class Poem:
    id: int
    dynasty: str                    # 仅构建期使用，不落库
    kind: str                       # 仅构建期使用，不落库
    title: str
    author: str                     # 佚名为 ""
    lines: List[str]
    rhythmic: str = ""              # 词牌 / 曲牌，诗为空串
    tags: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    strains: Optional[bytes] = None   # 4bit 打包后的平仄，长度与正文逐字对应
    strains_len: int = 0
    score: int = 0
    src_id: str = ""            # 源库 UUID，仅用于回溯，不参与检索
    src: str = ""               # 来源集合名，便于定位脏数据
    extra: dict = field(default_factory=dict)  # prologue / biography / chapter 等低频字段

    @property
    def body(self) -> str:
        return "\n".join(self.lines)

    @property
    def n_char(self) -> int:
        return sum(len(l) for l in self.lines)

    def to_json_obj(self, with_strains: bool = True) -> dict:
        """bundle 分片的落盘形态：省略空字段，短键名。

        with_strains=False 时丢弃 p/pl —— 平仄是可选包，主包里不该带着它。
        """
        o = {
            "i": self.id,
            "t": self.title,
            "a": self.author,
            "l": self.lines,
        }
        if self.rhythmic:
            o["r"] = self.rhythmic
        if self.tags:
            o["g"] = self.tags
        if self.notes:
            o["n"] = self.notes
        if self.score:
            o["s"] = self.score
        if with_strains and self.strains:
            o["p"] = self.strains.hex()   # hex 保证 JSON 安全
            o["pl"] = self.strains_len
        if self.extra:
            o["x"] = self.extra
        return o

    def to_strain_obj(self) -> Optional[dict]:
        """平仄可选包里单条记录的形态。只有挂接上平仄的记录才有输出。"""
        if not self.strains:
            return None
        return {"i": self.id, "p": self.strains.hex(), "pl": self.strains_len}

    @classmethod
    def from_json_obj(cls, o: dict) -> "Poem":
        d, k, _seq = parse_id(o["i"])
        p = cls(
            id=o["i"], dynasty=d, kind=k,
            title=o.get("t", ""), author=o.get("a", ""), lines=o.get("l", []),
            rhythmic=o.get("r", ""), tags=o.get("g", []), notes=o.get("n", []),
            score=o.get("s", 0),
            strains=bytes.fromhex(o["p"]) if o.get("p") else None,
            strains_len=o.get("pl", 0),
            extra=o.get("x", {}),
        )
        return p


# ---------------------------------------------------------------- bundle 字段表
BUNDLE_FIELDS = [
    ("i", "int32 全局 id，高 4 位朝代 / 中 3 位体裁 / 低 24 位序号"),
    ("t", "标题"),
    ("a", "作者"),
    ("l", "正文行数组（简体、无空格）"),
    ("r", "词牌 / 曲牌（可选）"),
    ("g", "标签数组（可选）"),
    ("n", "注释数组（可选）"),
    ("s", "热度 0-255（可选）"),
    # p / pl 只出现在「平仄可选包」里。主包不含平仄：平仄覆盖唐诗/宋诗 311,786 条
    # （全库 90.3%），词/曲/蒙学没有；主包带上它要为每个不需要平仄的用法多传字节。
    ("p", "平仄 hex，4bit/字，与正文逐字对应（仅平仄包）"),
    ("pl", "平仄符号数，用于解包截断（仅平仄包）"),
    ("x", "扩展字段 prologue/biography/chapter（可选）"),
]

# 平仄可选包的字段表（独立文件，独立 manifest）
STRAIN_BUNDLE_FIELDS = [
    ("i", "int32 全局 id，与主包 poems.id 一一对应"),
    ("p", "平仄 hex，4bit/字：0=仄 1=平 2=，3=。4=○ 5=？6=通 7=未知"),
    ("pl", "平仄符号数，用于解包时丢弃补齐的 nibble"),
]
