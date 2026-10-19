# -*- coding: utf-8 -*-
"""
诗词扩展属性层：现代汉语译文 / 注释 / 作品简介 / 创作背景 / 赏析 / 配图。

主库 poetry.db 只存「原文 + 最小元数据」，这六个字段是稀疏、大块、持续累积的补充内容。

# 为什么是独立 JSON sidecar 而不是往 poetry.db 里加一张表
1. 生命周期不同：主库是从源码重建出来的（跑一次 build.py 全量覆盖），
   extras 是不断累积的外部抓取 + 人工订正。独立目录天然免疫重建
   —— build.py 从不写 extras/，放进去的东西永远不会被冲掉。
2. 极度稀疏：34 万首里目前只有几百首有赏析。塞进 poems 主表等于让每行
   背上 6 个空字段；独立存储只写非空的部分。
3. 便于分发：整个 extras/ 目录可直接挂 CDN / 对象存储，消费方不用理解
   sqlite 结构，也不用为了读两条赏析去 ATTACH 一个 110 MB 的库。
4. 可审阅：JSONL 逐行可读，diff 友好，人工订正一行不会动到全表。

# 为什么内部用 JSONL 而不是一个漂亮的大 JSON
全量 json.dump 要把几十万条字符串同时在内存里拼好；JSONL 支持按行 seek、
流式追加、单条覆写。而且它同时就是交换格式——导入 / 导出用的同一个文件，
不需要再有第三套格式。

# 为什么图片只存**文件名**
存纯文件名（如 395f0f006b486c2f.jpg）而不是二进制，也不是带目录的相对路径：
  - 地址里不带目录前缀，消费方按固定约定（<extras 根>/images/）拼路径即可，
    不必先判断"这个前缀到底是 images/ 还是别的、要不要再拼一层"；
  - 整个 extras/ 目录可以整体搬走 / 同步到 CDN，地址依然有效；
  - 图片变了不用重写 JSONL；
  - 多个来源引用同一张图只占一份磁盘（按 URL sha1 去重）。

历史的 images/395f….jpg 写法在**读取侧仍然兼容**（image_abs 会认），
但写入侧一律压平成纯文件名（image_name）——避免两套写法长期并存。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema as S

# ---------------------------------------------------------------- 常量
EXTRAS_VERSION = "1.0"
JSONL = "extras.jsonl"              # 历史单文件名（仍可读取，但新写一律走分片）
JSONL_PART_GLOB = "extras-*.jsonl"  # 分片命名：extras-0001.jsonl / extras-0002.jsonl …
# 单片上限 60MB。why 分片：GitHub 单文件硬限 100MB，整份 extras 早就过百 MB，
# 不分片就永远进不了仓库（2026-10-19 把 extras 从被忽略的 dist/ 移出后仍提交不上，
# 才切成片）。留 40MB 余量，数据再涨一段也不用重新切。
JSONL_PART_MAX = 60 * 1024 * 1024
MANIFEST = "manifest.json"
IMAGE_DIR = "images"


def jsonl_parts(root: str) -> list:
    """
    返回该 extras 根下的 JSONL 分片路径（按名字升序）。

    why 兼容单文件：老目录里是 `extras.jsonl` 一个整文件，新目录是
    `extras-0001.jsonl` …。两边都要能读——否则历史数据一打开就变空库。
    单文件优先（它才是"一整份"），没有再退回分片。
    """
    single = os.path.join(root, JSONL)
    if os.path.exists(single):
        return [single]
    import glob as _glob
    return sorted(_glob.glob(os.path.join(root, JSONL_PART_GLOB)))

# 内容字段（短键）。存储用短键省字节，对外输出用长键（见 OUT_KEYS）。
FIELD_KEYS = ("tr", "nt", "in", "bg", "ap")

# 短键 -> 对外字段名
OUT_KEYS = {
    "tr": "translation",    # 现代汉语译文
    "nt": "notes",          # 注释
    "in": "introduction",   # 作品简介
    "bg": "background",     # 创作背景
    "ap": "appreciation",   # 赏析
}
OUT_KEY_INV = {v: k for k, v in OUT_KEYS.items()}

FIELD_LABELS = {
    "i": "诗的稳定 uid，与主库 poems.uid 一一对应（内容派生，重建不变；"
         "早于 schema 3.1 的老数据这里是数字 poems.id）",
    "tr": "translation  现代汉语译文",
    "nt": "notes        注释",
    "in": "introduction 作品简介",
    "bg": "background   创作背景",
    "ap": "appreciation 赏析",
    "im": "images       配图文件名数组（**纯文件名**，如 395f….jpg；本体在 images/ 目录，不含二进制）",
    "src": "source       来源标识（见 KNOWN_SOURCES），本条内容的首个来源；**不落库**，只留在 JSONL",
    "asrc": "alt sources  追加来源列表（一条记录的内容常有多个贡献方）；**不落库**",
    "lic": "license      许可协议；**不落库**",
    "url": "原始出处 URL（用于回溯与署名）；**不落库**",
    "u": "updated      最后更新时间（JSONL 里是本地时间 ISO，落库转成 Unix 整数秒）",
}

# ---------------- 落库列名（主库 poem_extras 表）----------------
# 为什么库里不用短键：短键是**存储**优化——JSONL 每一行都要把键名重写一遍，
# 九万多行乘下来不是零；而 SQLite 的列名是给人和给下游 SQL 看的，tr/nt/ap 这类
# 缩写会让每个消费方都不得不回来查一次对照表。所以：JSONL 存短键，库里存全名。
# 注意 bg 的库内列名是 creative_background（不是 background）——它表示"创作背景"，
# 与 translation / appreciation 一样用完整语义名。
#
# 为什么 src / asrc / lic / url 这四列**不落库**（v3.3 起）：
#   它们是**溯源元数据**，不是渲染内容——App 展示一首诗时用不到"这条赏析从哪个站
#   抓的""许可是 MIT 还是 CC BY"。而它们的基数还特别高（url 几乎一行一个），
#   留在库里既占地方又要单独维护索引。JSONL 是权威源，这四类信息在那儿一条不少，
#   真要溯源/导出署名时读 JSONL 即可（extras.py attribution 就是干这个的）。
# 只删列不删数据：JSONL 永远是完整的，库里只放消费侧真正要用的 7 列。
DB_COLUMNS = {
    "i": "uid",                    # 与主库 poems.uid 一一对应（字符串主键）
    "tr": "translation",           # 现代汉语译文
    "nt": "notes",                 # 注释
    "in": "introduction",          # 作品简介
    "bg": "creative_background",   # 创作背景
    "ap": "appreciation",          # 赏析
    "im": "images",                # 配图文件名数组（纯文件名，不带目录）
    "u": "updated",                # 最后更新时间（**Unix 整数秒**，见 to_epoch）
}

# 数组字段落库时用 \x1f 连接，与 poems.tags 是同一套约定。
# 不存 JSON 文本：库里混两种多值表示，下游每取一列都得先猜它是不是 JSON。
DB_MULTI = ("images",)
DB_SEP = "\x1f"

# updated 列的两种形态：JSONL 里是人类可读的本地时间 ISO 串，库里是整数秒。
# 生成端与解析端都用**本地时间**（time.strftime / time.mktime），两边一致，
# 所以不牵涉时区换算——这是内部时间戳，只用来比大小和排新,不需要跨时区对齐。
TS_FMT = "%Y-%m-%dT%H:%M:%S"


def to_epoch(v) -> int:
    """JSONL 的 `u`（本地时间 ISO，如 2026-09-29T16:31:26）-> Unix 秒（int）。

    why 库里存整数而不是原样存文本：
      - '2026-09-29T16:31:26' 是 19 字节，9.7 万行就是 ~1.1 MB 的纯字符串，换成
        int 后只占 1~4 字节；
      - 整数天然可比较、可做区间查询（`WHERE updated > ?`）。ISO 文本想比较就得
        依赖"字典序恰好等价于时间序"——对定宽格式成立，可一旦有行写成不带前导零
        或带时区后缀的形态，比较就会**静默**给出错误结果。
    解析不了的行返回 0，不抛错：一行脏时间不该让整表同步不了；0 也天然可辨认
    （1970-01-01 不会是真实的更新时间），排查时一眼能看出来。
    """
    if v is None or v == "":
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if s.isdigit():                     # 已经是秒（有人手写过，或者老的转换结果）
        return int(s)
    for fmt in (TS_FMT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return int(time.mktime(time.strptime(s[:19], fmt)))
        except ValueError:
            continue
    return 0


def from_epoch(v) -> str:
    """Unix 秒 -> 本地时间 ISO 串。读侧要给人看时间时用（库里存的是 int）。"""
    n = int(v or 0)
    return "" if n <= 0 else time.strftime(TS_FMT, time.localtime(n))


def to_db_row(rec: dict) -> dict:
    """JSONL 记录 -> poem_extras 行。列名一律用长名，缺失字段补空串。

    why 缺失补空串而不是留 NULL：读侧统一 `WHERE translation<>''` 就能筛"有译文的"，
    不用到处写 `IS NOT NULL AND <>''`——库里已经用空串表示"没有"，保持一致。
    updated 例外：它补 0（整数列用 0 表示"没记录时间"，与空串同一语义）。
    """
    row = {}
    for short, col in DB_COLUMNS.items():
        if col == "updated":
            continue                    # 单独处理成整数，见下
        v = rec.get(short)
        if isinstance(v, (list, tuple)):
            if col in DB_MULTI:
                v = [image_name(x) for x in v]      # 落库也保证是纯文件名
            row[col] = DB_SEP.join(str(x).strip() for x in v if str(x).strip())
        else:
            row[col] = "" if v is None else str(v).strip()
    row["updated"] = to_epoch(rec.get("u"))
    return row

# 已知来源登记表。why 单独登记而不是把许可串散落在每行里：
#   CC BY 一类的协议要求「署名 + 指向许可全文」。集中登记才能在导出 / 展示时
#   一次性给出正确的 attribution，也避免同一来源被抄成三四种写法。
KNOWN_SOURCES = {
    "poetry-collection": {
        "label": "open-chinese/poetry-collection",
        "license": "MIT",
        "home": "https://github.com/open-chinese/poetry-collection",
        "attribution": "AI-generated appreciation from open-chinese/poetry-collection (MIT)",
        "fields": ("ap",),
    },
    "shuge": {
        "label": "书格 shuge.org",
        "license": "CC BY 4.0",
        "home": "https://www.shuge.org/",
        "attribution": "图片来源：书格 shuge.org，公有领域古籍数字化资源，CC BY 4.0",
        "fields": ("im",),
    },
    "chagushici": {
        "label": "查古诗词 chagushici.com",
        # 为什么单独标"未声明"而不是当成可用：该站页面（含页脚）没有出现任何
        # CC / 公有领域之类的许可条款。译文、注释、鉴赏这类都是现代白话作品，
        # 默认受著作权保护。站内自用/研究可以，对外分发前需要另行取得授权——
        # 与其事后擦屁股，不如把字据留在 manifest 和 attribution 里。
        "license": "未声明开放许可",
        "home": "https://www.chagushici.com/",
        "attribution": "译文/注释/赏析/创作背景来源：查古诗词 chagushici.com。"
                       "该站未声明开放许可，仅建议自用或内部研究，对外分发前请先取得授权",
        "fields": ("tr", "nt", "in", "bg", "ap", "im"),
    },
    "manual": {
        "label": "人工录入 / 订正",
        "license": "user",
        "home": "",
        "attribution": "",
        "fields": FIELD_KEYS,
    },
    "llm": {
        "label": "大模型生成（本地调用）",
        "license": "AI-generated",
        "home": "",
        "attribution": "内容由本地大模型生成，未经人工校勘，仅供参考",
        "fields": FIELD_KEYS,
    },
}

DEFAULT_IMAGE_HEADERS = {
    # 部分图床按 UA 拒绝脚本请求；给一个常见 UA 而不是宣称自己是爬虫。
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}

_EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
}


class Rejected(Exception):
    """输入不合法。带明确原因，UI 直接显示给用户。"""


def _default_root() -> str:
    """默认目录按脚本位置解析，不依赖当前工作目录。

    why 不在 dist/ 下：dist/ 整个被 .gitignore 忽略，extras 是采集成果、
    重建不出来，放在那儿等于没有版本保护（2026-10-17 清 dist 时差点永久丢失）。
    """
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "extras")


def _guess_ext(url: str, content_type: str = "") -> str:
    """先从 Content-Type 拿，拿不到再退回 URL 后缀——图床常见 "?x-oss-process" 查询串。"""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _EXT_BY_TYPE:
        return _EXT_BY_TYPE[ct]
    path = urllib.parse.urlparse(url).path
    ext = os.path.splitext(path)[1].lower()
    return ext if ext and len(ext) <= 5 and ext.isascii() else ".jpg"


def image_name(ref) -> str:
    """
    任意形态的图片引用 -> **纯文件名**（不含任何目录信息）。

    `images/395f….jpg` / `images\\395f….jpg` / `395f….jpg` -> `395f….jpg`

    why 收敛到一种写法：目录前缀留在数据里，每个消费方都要先判断"要不要拼
    images/"、拼之前要不要去掉已有的前缀，判断错一次就是一张图显示不出来；
    而一旦允许前缀，`../` 这类穿越式引用也会跟着被写进来。所以约定成：
    **数据里只存文件名，目录由消费方按 IMAGE_DIR 补**。
    读取侧（image_abs）仍兼容历史的 images/ 前缀，写入侧一律经这里压平。
    """
    s = str(ref or "").strip().replace("\\", "/")
    return s.rsplit("/", 1)[-1].strip()


def _short(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# 常见图片格式的头部魔数
_MAGICS = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a",
           b"BM", b"RIFF", b"II*\x00", b"MM\x00*")


def _looks_like_image(blob: bytes) -> bool:
    """
    按文件头魔数判断是不是真图片。

    why 不能只信 Content-Type：图床 / CDN 出错时经常返回 200 + HTML 错误页，
    而 Content-Type 仍写着 image/jpeg。不校验的话 images/ 里会混进一堆名为
    .jpg 的 HTML，"配图"在界面上永远显示不出来，排查起来还特别绕。
    """
    if not blob:
        return False
    head = blob[:8]
    if any(head.startswith(m) for m in _MAGICS):
        return True
    low = blob.lstrip()[:12].lower()
    return low.startswith(b"<svg") or low.startswith(b"<?xml")


def _download_bytes(req, url: str, timeout: float, max_bytes: int, retries: int = 4):
    """
    带退避重试地取回 (Content-Type, bytes)。

    why 必须重试：批量抓图实测会撞上限流（连下两张书格后就返回 429）。
    一次性请求失败整张图就丢了，而批量任务里丢图往往很久之后才发现。
    429 / 503 是"过会儿再来就好"，退避重试能恢复；其他错误也重试一次防抖动。
    """
    last = ""
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ct = resp.headers.get("Content-Type", "")
                ct_kind = ct.split(";")[0].strip().lower()
                if ct and not ct_kind.startswith("image/"):
                    raise Rejected("目标不是图片（Content-Type=%s）：%s" % (ct, url))
                blob = resp.read(max_bytes + 1)
                if len(blob) > max_bytes:
                    raise Rejected("图片超过 %d 字节上限，已跳过：%s" % (max_bytes, url))
                return ct, blob
        except Rejected:
            raise                       # 内容层面的错误重试也没用，直接抛
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if i + 1 < retries:
                time.sleep(4 * (i + 1) if e.code in (429, 503) else 1.5 * (i + 1))
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, str(e)[:80])
            if i + 1 < retries:
                time.sleep(1.5 * (i + 1))
    raise Rejected("下载失败（重试 %d 次）：%s\n  最后错误：%s" % (retries, url, last))


# ---------------------------------------------------------------- 主类
class ExtrasStore:
    """
    extras 目录的读写口。

    读路径为「一次顺序建 offset 索引 + 按 pid seek」，所以即便文件涨到几万行，
    开库也只是扫一遍文件头而已——不需要把全部内容读进内存。
    写路径为「先改内存 + 显式 flush() 落盘」，因为 JSONL 没有原地改写的可能，
    多次修改攒成一次重写，比每条都重写整个文件便宜得多。
    """

    def __init__(self, root: str = None, cache_max: int = 2000):
        self.root = os.path.abspath(root or _default_root())
        self.jsonl = os.path.join(self.root, JSONL)   # 仅用于"是否存在"判断与兼容提示
        self.jsonl_parts = jsonl_parts(self.root)     # 实际读写的分片（可能就 1 个）
        self.imgdir = os.path.join(self.root, IMAGE_DIR)
        self.manifest_path = os.path.join(self.root, MANIFEST)

        self._index = {}                    # pid -> (分片下标, 文件字节偏移)
        self._cache = OrderedDict()         # pid -> dict（LRU，读多写少）
        self._dirty = set()                 # 改过但还没落盘的 pid
        self._deleted = set()               # 删了但还没落盘的 pid
        self._cache_max = max(1, cache_max)
        self._loaded = False

    # ---- 生命周期 ------------------------------------------------
    def ensure_dir(self):
        os.makedirs(self.imgdir, exist_ok=True)
        return self

    @property
    def exists(self) -> bool:
        return bool(self.jsonl_parts) or os.path.exists(self.jsonl)

    def load(self, force: bool = False):
        """
        建立 {pid: (分片下标, offset)} 索引。

        why 用 offset 而不是把整份 JSONL 读成 dict：三十万条的赏析文本能到几百 MB，
        全量驻留会直接把 GUI 变成内存杀手；而偏移索引只有 {pid: offset}，很轻
        （3.1 起 pid 是 uid 字符串，旧数据是 int，索引两种都收），
        真正的文本按需 seek 一行读出来。

        why 索引要带分片下标：整份数据切成多个 JSONL 之后，光有 offset 不知道
        该去哪个文件 seek，seek 到别的分片上就会读出一条张冠李戴的记录。
        """
        if self._loaded and not force:
            return self
        self._index = {}
        self._cache.clear()
        self._dirty.clear()
        self._deleted.clear()
        self.jsonl_parts = jsonl_parts(self.root)
        for pi, part in enumerate(self.jsonl_parts):
            with open(part, "r", encoding="utf-8") as f:
                while True:
                    off = f.tell()          # TextIOWrapper.tell() 返回 cookie，可安全回喂 seek()
                    line = f.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue            # 坏行跳过而不是让整个库打不开
                    pid = o.get("i")
                    # 3.1 起 pid 是 uid 字符串（内容派生的 uuid，形如 4a3c83d8-…）；
                    # 旧数据是 int。两种都收 —— 只认 int 的话，换成 uid 之后索引会
                    # 整个建不出来，所有查询静默返回 None，是最难查的一类问题。
                    if isinstance(pid, int) or (isinstance(pid, str) and pid):
                        self._index[pid] = (pi, off)    # (分片下标, 片内偏移)
        self._loaded = True
        return self

    def _evict(self):
        """
        把 LRU 挤到缓存上限以内——但**绝不能挤出脏记录**。

        dirty 的行还没落盘，它的唯一副本就在 _cache 里；一旦被挤出，
        flush() 会因为 "p not in self._cache" 跳过它，而 _index 里也没有偏移，
        这条改动就静默丢失了。        所以只淘汰干净行，全是脏的就停止淘汰，宁可缓存超标也不能丢数据。
        """
        while len(self._cache) > self._cache_max:
            victim = None
            for pid in self._cache:
                if pid not in self._dirty and pid not in self._deleted:
                    victim = pid
                    break
            if victim is None:
                break
            del self._cache[victim]

    def _touch(self, pid, rec):
        self._cache[pid] = rec
        self._cache.move_to_end(pid)
        self._evict()

    # ---- 读 ------------------------------------------------------
    def get(self, pid):
        if not self._loaded:
            self.load()
        if pid in self._deleted:
            return None
        if pid in self._cache:
            self._cache.move_to_end(pid)
            return self._cache[pid]
        loc = self._index.get(pid)
        if loc is None:
            return None
        pi, off = loc
        with open(self.jsonl_parts[pi], "r", encoding="utf-8") as f:
            f.seek(off)
            line = f.readline()
        if not line.strip():
            return None
        rec = json.loads(line)
        self._touch(pid, rec)
        return rec

    def batch(self, pids):
        """
        一次取多条，返回 {pid: rec}（只含有 extras 的 pid）。

        why 要按 offset 排序后顺序 seek：机械 / 系统页缓存都是顺序更快，
        乱序 seek 在一万条量级上能差出一倍时间。
        """
        if not self._loaded:
            self.load()
        out, miss = {}, []
        seen = set()
        for p in pids:
            if p in seen or p in self._deleted:
                continue
            seen.add(p)
            if p in self._cache:
                self._cache.move_to_end(p)
                out[p] = self._cache[p]
            elif p in self._index:
                miss.append(p)
        if miss:
            # 按 (分片, 片内偏移) 排序：同一片内顺序 seek，换片才重新开一次文件。
            # 不分片时这里就是原来那套"按偏移顺序扫一遍"的行为。
            for p in sorted(miss, key=lambda x: self._index[x]):
                pi, off = self._index[p]
                with open(self.jsonl_parts[pi], "r", encoding="utf-8") as f:
                    f.seek(off)
                    rec = json.loads(f.readline())
                self._touch(p, rec)
                out[p] = rec
        return out

    def decorated(self, rec, resolve_images: bool = True):
        """
        把内部短键展开成**长键名**，供 GUI / HTTP 接口直接渲染。
        rec 为 None 时返回 None，调用方不必到处判空。

        why 两套键名并存：存储用短键（tr/nt/ap…）是为了省字节——每条赏析几百字，
        键名省下的比例虽然小但乘三十万条不是零；对外输出用长键是因为那是给人
        和给别的程序看的，translation 比 tr 少一次查文档。
        """
        if not rec:
            return None
        out = {"poem_id": rec.get("i")}
        for short, long_name in OUT_KEYS.items():
            out[long_name] = rec.get(short, "")
        imgs = rec.get("im") or []
        # 对外输出也只给纯文件名：老数据里可能还留着 images/ 前缀（迁移前），
        # 这里顺手压平，调用方拿到的 shapes 始终一致。
        out["images"] = [n for n in (image_name(x) for x in imgs) if n]
        out["source"] = rec.get("src", "")
        out["alt_sources"] = rec.get("asrc") or []
        out["license"] = rec.get("lic", "")
        out["url"] = rec.get("url", "")
        out["updated"] = rec.get("u", "")
        if resolve_images and imgs:
            out["image_paths"] = [self.image_abs(x) for x in imgs]
        return out

    # ---- 写 ------------------------------------------------------
    @staticmethod
    def _canon(fields: dict) -> dict:
        """
        把调用方可能传来的**长键名**（translation / appreciation …）归一成内部短键。

        why 必须兼容两种：decorated() 对外输出的是长键名，调用方很自然会把整份
        dict 原样喂回 put()。只认短键的话这种调用会**静默一个字都不写**——
        不报错、数据也没进去，是所有 bug 里最难发现的一类（实测踩过：
        GUI「扩展资料」保存后界面毫无变化，但没有任何异常抛出）。
        兼容两种键名的成本几乎为零。
        """
        out = dict(fields)
        for long_name, short in OUT_KEY_INV.items():
            v = out.pop(long_name, None)
            if v is not None and short not in out:
                out[short] = v
        return out

    def put(self, pid, fields: dict, source: str = "", lic: str = "",
            url: str = "") -> dict:
        """
        写入 / 合并一条 extras。返回合并后的整条记录。

        合并策略：**只有非空值才覆盖**。
        why：多个来源各自只填得到一部分字段——A 来源给译文、B 来源给赏析，
        第二次 put 不该把第一次的译文擦成空串。想显式清空请用 remove_field()。
        """
        if not self._loaded:
            self.load()
        fields = self._canon(fields or {})
        rec = dict(self.get(pid) or {"i": pid})
        for k in FIELD_KEYS:
            v = fields.get(k)
            if v is not None and str(v).strip():
                rec[k] = str(v).strip()
        imgs = fields.get("im")
        if isinstance(imgs, (list, tuple)):
            merged = list(rec.get("im") or [])
            for x in imgs:
                # 一律压平成纯文件名：调用方（GUI 输入框 / 外部导入）可能传
                # `images/x.jpg` 甚至带路径的写法，存进 JSONL 之前收敛掉，
                # 免得同一张图在库里出现两种写法。
                x = image_name(x)
                if x and x not in merged:
                    merged.append(x)
            if merged:
                rec["im"] = merged
        # 来源登记：一条记录的内容常常来自多个来源（赏析来自 A、配图来自 B）。
        # 所以**保留首个来源**作为 src/lic（它通常是内容主体），后来的来源追加到
        # asrc 里，不让它们悄悄顶掉前面的署名——CC BY 类协议是要求署名的，
        # 把 MIT 的赏析标成别家的 CC BY 是实打实的合规问题。
        if source:
            old = rec.get("src")
            if old and old != source:
                alts = list(rec.get("asrc") or [])
                if source not in alts:
                    alts.append(source)
                    rec["asrc"] = alts
            else:
                rec["src"] = source
        if lic:
            rec["lic"] = lic
        if url:
            rec["url"] = url
        rec["u"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._deleted.discard(pid)
        self._cache[pid] = rec
        self._cache.move_to_end(pid)
        self._dirty.add(pid)
        self._evict()
        return rec

    def remove_field(self, pid, key: str) -> bool:
        """显式清掉某个字段（如删掉一张错配的图）。整条空了会自动删除。"""
        if not self._loaded:
            self.load()
        key = OUT_KEY_INV.get(key, key)     # 同样兼容长键名
        rec = self.get(pid)
        if not rec:
            return False
        rec.pop(key, None)
        if not [k for k in FIELD_KEYS + ("im",) if rec.get(k)]:
            self._deleted.add(pid)
            self._cache.pop(pid, None)
        else:
            self._cache[pid] = rec
            rec["u"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._dirty.add(pid)
        return True

    def delete(self, pid) -> bool:
        if not self._loaded:
            self.load()
        if pid not in self._index and pid not in self._cache:
            return False
        self._deleted.add(pid)
        self._cache.pop(pid, None)
        return True

    def flush(self) -> int:
        """
        把内存改动重写落盘，返回最终条数。

        why 整文件重写而不是在行内原地改：JSONL 一行长度可变，改一个字就可能
        写越界覆盖下一行。先写 .tmp 再 os.replace 还能顺带拿到原子性——
        中途崩了不会留下半个损坏的 extras.jsonl。
        """
        if not self._loaded:
            self.load()
        if not self._dirty and not self._deleted:
            return len(self._index)

        rows = {}
        # 1) 磁盘上「没被删、且缓存里的版本不是最新」的行，原样搬到内存
        keep = sorted(set(self._index) - self._deleted - self._dirty,
                      key=lambda p: self._index[p])
        for p in keep:
            pi, off = self._index[p]
            with open(self.jsonl_parts[pi], "r", encoding="utf-8") as f:
                f.seek(off)
                line = f.readline().strip()
            if line:
                rows[p] = json.loads(line)
        # 2) 缓存里的最新版本盖上去了
        for p in list(self._dirty):
            if p in self._deleted or p not in self._cache:
                continue
            rows[p] = self._cache[p]

        os.makedirs(self.root, exist_ok=True)
        # 3) 按大小切片重写。why 不写单个大文件：GitHub 单文件 100MB 硬限，
        #    超了就提交不上去；每次 flush 都重新切，数据量涨了也不会悄悄超限。
        #    旧的整文件 extras.jsonl 与多余分片在写完后清掉，避免新旧混读。
        new_index = {}
        tmp_names = []
        f = None
        pi = -1
        cur_bytes = 0
        for p in sorted(rows, key=str):     # key=str：uid/int 混排时 sorted 会抛 TypeError
            line = _short(rows[p]) + "\n"
            b = len(line.encode("utf-8"))
            if f is None or (cur_bytes + b > JSONL_PART_MAX and cur_bytes > 0):
                if f is not None:
                    f.close()
                pi += 1
                name = "extras-%04d.jsonl" % (pi + 1)
                tmp = os.path.join(self.root, name + ".tmp")
                tmp_names.append((tmp, name))
                f = open(tmp, "w", encoding="utf-8")
                cur_bytes = 0
            new_index[p] = (pi, f.tell())
            f.write(line)
            cur_bytes += b
        if f is not None:
            f.close()
        for tmp, name in tmp_names:
            os.replace(tmp, os.path.join(self.root, name))
        # 清掉不再属于当前分片集合的旧文件（含历史的整份 extras.jsonl）
        keep_names = {name for _, name in tmp_names}
        for old in jsonl_parts(self.root):
            if os.path.basename(old) not in keep_names:
                os.remove(old)

        self._index = new_index
        self.jsonl_parts = [os.path.join(self.root, n) for _, n in tmp_names]
        self._dirty.clear()
        self._deleted.clear()
        return len(self._index)

    def __len__(self):
        if not self._loaded:
            self.load()
        return len(self._index)

    def iter_records(self):
        """顺序遍历全部记录（用于导出 / 统计）。不进 LRU 缓存，避免冲掉热点数据。"""
        if not self._loaded:
            self.load()
        for part in self.jsonl_parts:
            with open(part, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        yield json.loads(line)

    # ---- 图片 ----------------------------------------------------
    def image_abs(self, rel: str) -> str:
        """
        图片引用 -> 绝对路径（永远落在 <root>/images/ 下），并做目录穿越防护。

        **存储约定是纯文件名**（如 395f….jpg）；这里额外兼容历史的
        `images/395f….jpg` 写法，让老数据不用先迁移也能正常显示。

        why 只认 IMAGE_DIR 这一层前缀、别的目录一律拒：
          - rel 来自 JSONL 或外部导入文件，放行任意相对路径就等于把
            「打开配图」变成任意文件读取（`../../Windows/…`）；
          - 绝对路径虽然在常见系统上跳不出去，却会让「目录可整体搬走 / 挂 CDN」
            这个前提悄悄失效——配图一旦指向目录外的文件，搬走就全断。
        """
        raw = str(rel or "").replace("\\", "/").strip()
        if not raw:
            raise Rejected("空图片地址")
        head, sep, tail = raw.rpartition("/")
        if sep:
            if head != IMAGE_DIR:
                raise Rejected("非法图片地址（配图只应存文件名，不带目录）：%s" % raw)
            raw = tail                      # 历史写法 images/x.jpg -> x.jpg
        if not raw or raw in (".", ".."):
            raise Rejected("非法图片地址：%s" % rel)
        if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
            raise Rejected("非法图片地址（不允许绝对路径）：%s" % rel)
        p = os.path.normpath(os.path.join(self.root, IMAGE_DIR, raw))
        if os.path.commonpath([p, self.root]) != self.root:
            raise Rejected("非法图片地址（不允许跳出 extras 目录）：%s" % rel)
        return p

    def save_image(self, url: str, referer: str = "", timeout: float = 30,
                   max_bytes: int = 12 * 1024 * 1024):
        """
        下载一张图到 <root>/images/，返回 (**纯文件名**, 是否新下载)。

        文件名 = URL 的 sha1 前 16 位 + 后缀：同一 URL 只会存一份，
        多个来源引用同一张图不占两份空间，重复跑脚本也不会累积副本。
        返回的是文件名而不是 `images/xxx.jpg`——存进 JSONL 的值不带目录，
        目录由读取侧按 IMAGE_DIR 补（见 image_name / image_abs）。
        """
        url = str(url).strip()
        if not url.lower().startswith(("http://", "https://")):
            raise Rejected("不是 http(s) 地址：%s" % url)
        os.makedirs(self.imgdir, exist_ok=True)
        ext = _guess_ext(url)
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        name = "%s%s" % (key, ext)
        dest = os.path.join(self.imgdir, name)
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            return name, False          # 已存在：命中本地缓存，不再联网

        req = urllib.request.Request(url, headers=dict(DEFAULT_IMAGE_HEADERS))
        if referer:
            req.add_header("Referer", referer)
        ct, blob = _download_bytes(req, url, timeout, max_bytes)
        # 拿到真实 Content-Type 后重判后缀：URL 里没有后缀、或者带 ?x-oss-process
        # 这类改写参数时，之前按 URL 猜的扩展名很可能是错的。
        ext = _guess_ext(url, ct)
        name = "%s%s" % (key, ext)
        dest = os.path.join(self.imgdir, name)
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            return name, False
        # 校验魔数：Content-Type 可能撒谎，扩展名也可能被写成 .jpg 实际是 HTML 错误页
        if not _looks_like_image(blob):
            raise Rejected("下载到的不是图片（可能是错误页面）：%s" % url)
        with open(dest, "wb") as f:
            f.write(blob)
        return name, True

    # ---- 统计 / manifest -----------------------------------------
    def stats(self) -> dict:
        if not self._loaded:
            self.load()
        fields = {k: 0 for k in FIELD_KEYS}
        sources, n_img = {}, 0
        for rec in self.iter_records():
            for k in FIELD_KEYS:
                if rec.get(k):
                    fields[k] += 1
            for s in [rec.get("src")] + list(rec.get("asrc") or []):
                if s:
                    sources[s] = sources.get(s, 0) + 1
            n_img += len(rec.get("im") or [])
        n_files = 0
        total = 0
        if os.path.isdir(self.imgdir):
            for fn in os.listdir(self.imgdir):
                fp = os.path.join(self.imgdir, fn)
                if os.path.isfile(fp):
                    n_files += 1
                    total += os.path.getsize(fp)
        return {
            "version": EXTRAS_VERSION,
            "n_records": len(self._index),
            "fields": fields,
            "sources": sources,
            "images": {"refs": n_img, "files": n_files, "bytes": total},
        }

    def write_manifest(self):
        st = self.stats()
        mf = {
            "schema": EXTRAS_VERSION,
            "compat_schema": S.SCHEMA_VERSION,   # 针对哪个主库版本产出的
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "root": os.path.basename(self.root),
            "fields": FIELD_LABELS,
            "image_dir": IMAGE_DIR,
            "count": st["n_records"],
            "field_counts": st["fields"],
            "sources": [
                {"name": k,
                 "license": (KNOWN_SOURCES.get(k) or {}).get("license", "unknown"),
                 "home": (KNOWN_SOURCES.get(k) or {}).get("home", ""),
                 "attribution": (KNOWN_SOURCES.get(k) or {}).get("attribution", ""),
                 "records": v}
                for k, v in sorted(st["sources"].items(), key=lambda x: -x[1])
            ],
            "images": st["images"],
        }
        os.makedirs(self.root, exist_ok=True)
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            json.dump(mf, f, ensure_ascii=False, indent=2)
        return mf


# ---------------------------------------------------------------- 挂接到主库
def match_poem(con, title: str, author: str = "", first_line: str = ""):
    """
    把「只有标题 / 作者 / 正文」的外部数据挂到库里已有诗词的 poem_id 上。
    三级匹配，从严到宽，全部失败返回 None。

    why 三级而不是只按 (title, author)：外部数据的作者写法和库里经常不一致
    （「李白」vs「唐·李白」、繁简差异），只靠精确匹配会丢掉大量本该命中的条目。
    第三级退化到只按标题时也只取 id 最小的一条——标题重复率实测 1.26 次/值，
    绝大多数情况下足够安全，且宁可挂到一首也不愿整条数据被丢掉。
    """
    t = S.norm_text(title or "").strip()
    if not t:
        return None
    a = S.norm_author(author or "").strip()
    if a:
        row = con.execute(
            "SELECT p.id FROM poems p JOIN authors au ON au.uid=p.author_uid "
            "WHERE p.title=? AND au.name=? ORDER BY p.id LIMIT 1", (t, a)).fetchone()
        if row:
            return row[0]
    fl = S.norm_text((first_line or "").strip())
    if fl:
        row = con.execute(
            "SELECT id FROM poems WHERE title=? AND body LIKE ? ORDER BY id LIMIT 1",
            (t, fl[:12] + "%")).fetchone()
        if row:
            return row[0]
    row = con.execute("SELECT id FROM poems WHERE title=? ORDER BY id LIMIT 1",
                      (t,)).fetchone()
    return row[0] if row else None


def attribution_lines(store: ExtrasStore) -> list:
    """生成对外分发时必须带上的署名清单（CC BY 类来源是硬性要求）。"""
    st = store.stats()
    out = []
    for name, cnt in sorted(st["sources"].items(), key=lambda x: -x[1]):
        info = KNOWN_SOURCES.get(name)
        if not info:
            out.append("%s（%d 条，来源未登记，使用前请自行确认许可）" % (name, cnt))
            continue
        out.append("%s —— %s，%d 条。%s" % (
            info["label"], info["license"], cnt, info["attribution"]))
    return out


def normalize_image_names(root: str = None, dry_run: bool = False) -> dict:
    """
    把 extras.jsonl 里 `im` 的历史写法（`images/395f….jpg`）统一成**纯文件名**。

    why 单独一个流式重写而不是走 ExtrasStore.put()：
    put 只写"脏"记录，要迁移全表就得把 9.7 万条全标脏、全塞进 LRU 缓存，
    内存直接爆；而这里只动每行的 im 字段，逐行读写的内存占用是常数级。

    备份：真的有改动时先写 extras.jsonl.bak-<时间戳>，再原子替换。
    幂等：已是纯文件名的行原样写出，重复跑第二遍 changed=0。
    """
    store = ExtrasStore(root)
    src = store.jsonl
    if not os.path.exists(src):
        raise Rejected("找不到 %s" % src)
    tmp = src + ".tmp"
    n_lines = n_changed = n_refs = 0
    with open(src, "r", encoding="utf-8") as fin, \
            open(tmp, "w", encoding="utf-8") as fout:
        for line in fin:
            s = line.strip()
            if not s:
                continue
            try:
                rec = json.loads(s)
            except json.JSONDecodeError:
                fout.write(line if line.endswith("\n") else line + "\n")
                continue                    # 坏行原样保留，不替用户做决定
            n_lines += 1
            imgs = rec.get("im")
            if isinstance(imgs, list):
                fixed = [n for n in (image_name(x) for x in imgs) if n]
                # 去重但保序（同一张图可能因两种写法被记了两次）
                seen, dedup = set(), []
                for n in fixed:
                    if n not in seen:
                        seen.add(n)
                        dedup.append(n)
                n_refs += len(dedup)
                if dedup != imgs:
                    n_changed += 1
                if dedup:
                    rec["im"] = dedup
                else:
                    rec.pop("im", None)
            fout.write(_short(rec) + "\n")
    if n_changed == 0 and not dry_run:
        os.remove(tmp)
    return {"file": src, "lines": n_lines, "changed": n_changed,
            "image_refs": n_refs, "tmp": tmp, "dry_run": dry_run}


def apply_normalize_image_names(root: str = None) -> dict:
    """normalize_image_names 的落盘版：备份 -> 原子替换 -> 重写 manifest。"""
    import shutil
    info = normalize_image_names(root, dry_run=True)
    if info["changed"] == 0:
        os.remove(info["tmp"])
        return dict(info, backup="")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = "%s.bak-%s" % (info["file"], stamp)
    shutil.copy2(info["file"], backup)
    os.replace(info["tmp"], info["file"])
    ExtrasStore(root).load(force=True).write_manifest()
    return dict(info, backup=backup, dry_run=False)


# ---------------------------------------------------------------- CLI
def main():
    import argparse
    p = argparse.ArgumentParser(description="extras 扩展属性层")
    p.add_argument("--root", default=None, help="extras 目录，默认 ./extras")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("stats", help="查看统计")
    sub.add_parser("manifest", help="重写 manifest.json")

    q = sub.add_parser("get", help="查一条")
    q.add_argument("pid", type=int)

    d = sub.add_parser("rm", help="删一条")
    d.add_argument("pid", type=int)

    e = sub.add_parser("export", help="导出为单个 JSON 数组（便于审阅 / 交付）")
    e.add_argument("path")

    a = sub.add_parser("attribution", help="输出必须保留的署名清单")

    n = sub.add_parser("normalize-images",
                       help="把 im 里的 images/xxx.jpg 统一成纯文件名 xxx.jpg")
    n.add_argument("--apply", action="store_true",
                   help="真正写回（默认只演练，不落盘）")

    args = p.parse_args()
    if args.cmd == "normalize-images":
        info = (apply_normalize_image_names(args.root) if args.apply
                else normalize_image_names(args.root, dry_run=True))
        if not args.apply:
            os.remove(info["tmp"])
        print(json.dumps({k: v for k, v in info.items() if k != "tmp"},
                         ensure_ascii=False, indent=2))
        return
    st = ExtrasStore(args.root).load()
    if args.cmd == "stats":
        print(json.dumps(st.stats(), ensure_ascii=False, indent=2))
    elif args.cmd == "manifest":
        print(json.dumps(st.write_manifest(), ensure_ascii=False, indent=2))
    elif args.cmd == "get":
        print(json.dumps(st.decorated(st.get(args.pid)), ensure_ascii=False, indent=2))
    elif args.cmd == "rm":
        print("已删除" if st.delete(args.pid) else "不存在", "; flush=%d" % st.flush())
    elif args.cmd == "export":
        rows = [st.decorated(r) for r in st.iter_records()]
        with open(args.path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        print("已导出 %d 条 -> %s" % (len(rows), args.path))
    elif args.cmd == "attribution":
        for line in attribution_lines(st):
            print(" * " + line)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
