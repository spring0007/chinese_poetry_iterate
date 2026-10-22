# -*- coding: utf-8 -*-
"""体裁标注（genre tagging）——给八个朝代文件夹的每条诗词记录打上唯一、稳定的体裁 ID。

覆盖集合（共 13,808 条，11 个分片文件）：

    南北朝 299 / 明 299 / 汉 267 / 近现代 11,848 / 金 292 / 隋 289 / 魏晋 299 / 清诗 215

体裁表（跨文件夹共用同一套 ID；登记的权威定义在 `schema.GENRES`，本文件只查表，
不再另存一份——两份体裁表迟早分叉）：

    id  code     名称   说明
    1   shi      诗     古体、近体、乐府——齐言或杂言，允许带小序
    2   ci       词     按词牌填写，长短句；词牌取自 宋词 语料 + 常见别名
    3   qu       曲     散曲、套数、剧曲；含【曲牌】/〔曲牌〕标记
    4   fu       赋     以“赋”名篇（骚体赋、骈赋、文赋）
    5   sao      骚     楚辞体，多用“兮”字（离骚、九歌、九章…）
    6   wen      文     散文、骈文与应用文体（书序记铭论表疏诏…）
    7   xinshi   新诗   近现代自由体诗
    8   lian     联     联语（对联、长联）

用法：

    python genre_tag.py                    # 只试标，打印分布与抽样，不写文件
    python genre_tag.py --samples 12       # 试标 + 每类抽 12 条看
    python genre_tag.py --folder 金        # 只看某一集
    python genre_tag.py --apply            # 备份 → 原地写入 → 逐条校验
    python genre_tag.py --apply --folder 金
    python genre_tag.py --verify           # 校验已写入的标注

源数据里**只写 genreId**（v3.5 起）：体裁的中文名不进 JSON，它是字典表的数据，
落在 `genres.json` 与 SQLite 的 `genre(id, name)` 里（3.5 那版临时寄存在 source 表；
3.6 期间 source 改作朝代表，3.7 已把 source 整张删掉，体裁只剩 genre 一张）。一条记录
里既写 id 又写名称，等于同一事实存两处，改一处就分叉，所以 `--apply` 会把老版本写
进去的 `"genre"` 键顺手摘掉。

写文件是**文本级外科编辑**：只在该条记录最后一个字段后面插入
`, \n<缩进>"genreId": N`，不动其余任何字节。八个文件夹的缩进（1/2/4 空格）与
换行符（CRLF/LF）各不相同，全部按文件实测值套用，因此 diff 里只会多出一行。

判定是**规则+特征**的启发式，不是分类模型。核心特征：按句读（，。；！？、：）
切分后的分句长度分布（三/四/五/七言占比）、最长分句、文言虚词密度（者也矣焉哉曰）、
白话词密度（的了着吗呢吧啊）、“兮”字密度，再加标题上的体裁线索（词牌/曲牌/文体尾字）。
逐条人工复核过小集（南北朝/明/汉/金/隋/魏晋/清诗），大集（近现代）按类抽样复核。
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import random
import re
import shutil
import sys
import tarfile
import time

# ---------------------------------------------------------------- 体裁登记表
# 权威定义只有一处：schema.GENRES（建库时用它生成 SQLite 的 genre 表）。
# 这里 import 而不是抄一份——两份体裁表只要存在，就一定会在某次改动后分叉，
# 而分叉的表现是「源数据说 2、库里 name 是 曲」这种极难排查的静默错位。
from schema import GENRES

BY_ID = {g[0]: g for g in GENRES}
BY_NAME = {g[2]: g for g in GENRES}

FOLDERS = ["南北朝", "明", "汉", "近现代", "金", "隋", "魏晋", "清诗"]
# 唐前四集没有严格意义的“词”（隋有 1 首《酒泉子》），词牌匹配要额外要求“非齐言”，
# 否则《子夜歌》《折杨柳》这类乐府会被词牌表带偏。
TANGQIAN = ("南北朝", "汉", "魏晋", "隋")
# 曲只在有散曲/剧曲实绩的四集里判定
QU_FOLDERS = ("金", "明", "清诗", "近现代")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(ROOT, "genres.json")
BACKUP_DIR = os.path.join(ROOT, "poetry-pipeline", "dist", "backups")

# ---------------------------------------------------------------- 词表/正则

# 词牌：宋词语料里的 rhythmic 字段（1,421 个）+ 语料里没出现但确实存在的别名。
CI_EXTRA = [
    "浣溪纱", "减兰", "贺新凉", "百字令", "大江东去", "金缕曲", "望江南", "凤栖梧",
    "一剪梅", "鹊桥仙", "无俗念", "下手迟", "报师恩", "忍辱仙人", "望蓬莱", "清心镜",
    "爇心香", "神光灿", "蓬莱阁", "金莲出玉花", "黄鹤洞中仙", "三奠子", "无漏子",
    "玉炉三涧雪", "戚氏", "梅花引", "人月圆", "诉衷情令", "贺圣朝", "促拍丑奴儿",
    "青杏儿", "寿楼春", "催雪", "青玉案", "南歌子", "霜天晓角", "满路花", "步蟾宫",
    "祝英台近", "喜迁莺", "双双燕", "满庭芳", "水龙吟", "沁园春", "临江仙", "念奴娇",
    "清平乐", "忆秦娥", "定风波", "苏幕遮", "渔家傲", "江城子", "少年游", "南乡子",
    "玉楼春", "踏莎行", "千秋岁", "风入松", "齐天乐", "扬州慢", "兰陵王", "暗香",
    "疏影", "八声甘州", "贺新郎", "洞仙歌", "汉宫春", "醉花阴", "声声慢", "武陵春",
    "采桑子", "诉衷情", "生查子", "阮郎归", "昭君怨", "减字木兰花", "木兰花慢",
    "高阳台", "渡江云", "解连环", "绮罗香", "瑞鹤仙", "摸鱼儿", "钗头凤", "唐多令",
    "行香子", "六州歌头", "夜半乐", "眼儿媚", "惜分飞", "木兰花", "醉蓬莱",
    "法曲献仙音", "湘月", "淡黄柳", "长亭怨慢", "翠楼吟", "琵琶仙", "惜红衣",
    "石州慢", "扫花游", "琐窗寒", "三姝媚", "瑞龙吟", "莺啼序", "庆清朝", "雨中花",
    "蓦山溪", "青门引", "天仙子", "醉落魄", "一斛珠", "踏歌词", "忆少年", "卖花声",
    "桂殿秋", "解佩令", "一丛花", "于中好", "太常引", "山花子", "相见欢", "如此江山",
    "苏武慢", "烛影摇红", "柳梢青", "凄凉犯", "杏花天", "御街行", "九张机", "破阵子",
    # 短调与冷僻调：宋词语料里没有，但确实按词牌写
    "十六字令", "苍梧谣", "归字谣", "字字双", "一七令", "摘得新", "花非花", "梧桐影",
    "晴偏好", "醉妆词", "一片子", "甘州曲", "渔父引", "啰唝曲", "九回肠",
    "西楼月", "月上瓜洲", "凌波曲", "秋蕊香", "平湖乐", "小桃红", "快活年", "殿前欢",
    "沽美酒", "寿阳曲", "落梅风", "水仙子", "清江引", "折桂令", "天净沙", "山坡羊",
    "驻马听", "步步娇", "雁儿落", "得胜令", "胡十八", "大德歌", "拨不断", "四块玉",
    "南楼令", "踏莎美人", "琴调相思引", "三台令", "甘州", "甘州令", "八宝妆", "月华清",
    "金人捧露盘", "玲珑四犯", "貂裘换酒", "壶中天", "湘春夜月", "瑶华", "无闷", "天香",
    "花犯", "眉妩", "澡兰香", "绣鸾凤", "换巢鸾凤", "东风第一枝", "庆春宫",
    "拜星月慢", "尉迟杯", "瑞鹤仙影", "江梅引", "小梅花", "明月引", "西湖明月引",
    "国香", "国香慢", "望月婆罗门引", "婆罗门引",
    # 别名：鹊踏枝=蝶恋花、梦江南=忆江南、东风齐着力=东风齐著力
    "鹊踏枝", "梦江南", "东风齐着力", "东风齐著力", "江月晃重山", "步虚词",
    "丑奴儿令", "丑奴儿近", "添字丑奴儿", "转调丑奴儿",
]
# 曲牌：元曲语料标题斜杠后的部分（45 个）+ 常用曲牌
QU_EXTRA = [
    "朝天子", "山坡羊", "水仙子", "驻云飞", "锁南枝", "折桂令", "天净沙", "沉醉东风",
    "叨叨令", "寄生草", "一半儿", "清江引", "四块玉", "拨不断", "寿阳曲", "落梅风",
    "雁儿落", "得胜令", "沽美酒", "太平令", "醉太平", "脱布衫", "小梁州", "石榴花",
    "斗鹌鹑", "上小楼", "普天乐", "喜春来", "迎仙客", "红绣鞋", "朱履曲", "端正好",
    "滚绣球", "倘秀才", "呆骨朵", "快活三", "鲍老儿", "货郎儿", "骤雨打新荷",
    "青杏子", "湘妃怨", "楚天遥", "殿前欢", "夜行船", "乔木查", "庆宣和", "风入松",
    "雁过声", "梁州第七", "骂玉郎", "感皇恩", "采茶歌", "乌夜啼", "斗黑麻", "玉交枝",
    "四边静", "川拨棹", "七弟兄", "梅花酒", "收江南",
    # 昆曲/南曲常用曲牌
    "皂罗袍", "玉芙蓉", "绕池游", "懒画眉", "步步娇", "忒忒令", "好姐姐",
    "滴溜子", "江儿水", "园林好", "锦衣香", "浆水令", "古蟾宫", "山丹花",
]
SAO_NAMES = ("离骚", "九歌", "九章", "九辩", "九怀", "九叹", "九思", "七谏", "招魂",
             "大招", "卜居", "渔父", "天问", "远游", "惜誓", "哀时命", "九守",
             # 骚体名篇：正文把“兮”讹作“为”，靠“兮”字数抓不到，只能按篇名收
             "胡笳十八拍", "琴曲歌辞十二操")

# 文体尾字：标题以这些字收尾，基本可判文（个别例外由 body 特征翻案）
WEN = re.compile(
    r"(书|序|记|铭|箴|赞|颂|诔|碑|志|传|论|表|疏|诏|令|檄|策|说|解|启|笺|状|言|世家|"
    r"列传|本纪|墓志铭|行状|神道碑|哀辞|祭文|吊文|连珠|引|疏证|文|辞)(并序|序)?$")
# 诗体尾字：…行/篇/吟/歌/谣/操/辞
SHI_TAIL = re.compile(r"(杂言诗|七律|七绝|五律|五绝|排律|古风|歌行|行|篇|吟|歌|谣|操|辞)$")
# “记事型”题尾：…醉书/杂书/漫书 —— 看着像“书”，其实是写诗
POEM_TAIL = re.compile(r"(醉书|杂书|漫书|闲书|信笔书|书所见|书怀|书事|书感|书愤|书闷|书兴)$")
# 标题开头的体裁强标记：六言诗·…、七言…、杂言诗…（必须先于 WEN，否则“同志”的“志”会判文）
SHI_MARK = re.compile(r"^(七律|七绝|五律|五绝|七言|五言|四言|六言|三言|排律|古风|歌行|杂言诗)")
PROSE = re.compile(r"[者也矣焉哉曰]")          # 文言虚词
BAI = re.compile(r"[的了着吗呢吧啊]")           # 白话词
CL = re.compile(r"[，。；！？、：,.;!?:]")       # 句读
# 文言叙事连接词：出现三次以上、且非齐言，基本是散文（《口技》这类）
PROSE_MARK = re.compile(r"(少顷|既而|未几|俄而|须臾|忽闻|但闻|于是|遂命|乃命)")
# 正文里的曲牌标记〔北新水令〕/【双调】
QU_MARK = re.compile(r"[〔【][^〕】]{2,8}[〕】]")
# 赋的排除：这些是“赋诗/赋得”的动词用法，不是赋体
FU_EXCLUDE = re.compile(
    r"(赋此|赋示|赋呈|赋赠|赋答|赋谢|赋别|感赋|赋韵|为.*赋|归赋|中赋|夜赋|后赋|前赋|"
    r"偶赋|漫赋|戏赋|醉赋|梦赋|再赋|重赋|同赋)$")

PAI: set[str] = set()
RARE: set[str] = set()
CI_CNT: dict[str, int] = {}
QU: set[str] = set()


def _tables(root: str = ROOT) -> None:
    """从同项目语料里提取词牌/曲牌表。语料缺失时降级为内置别名表。"""
    global PAI, RARE, QU, CI_CNT
    cnt: collections.Counter[str] = collections.Counter()
    for f in glob.glob(os.path.join(root, "宋词", "ci.song.*.json")):
        with open(f, encoding="utf-8-sig") as fh:
            for r in json.load(fh):
                if r.get("rhythmic"):
                    cnt[r["rhythmic"]] += 1
    # 只出现 1~2 次的词牌，多半是“以题代词牌”（如《春晴》其实是七律题），
    # 同名但正文是齐言四/八句时要二次确认，见 classify() 里的 rare 分支。
    RARE = {k for k, v in cnt.items() if v <= 2}
    CI_CNT = dict(cnt)
    PAI = set(cnt) | set(CI_EXTRA)
    qu: set[str] = set()
    yuan = os.path.join(root, "元曲", "yuanqu.json")
    if os.path.exists(yuan):
        with open(yuan, encoding="utf-8-sig") as fh:
            for r in json.load(fh):
                t = r.get("title") or ""
                if "/" in t:
                    qu.add(t.split("/")[-1].strip())
    QU = qu | set(QU_EXTRA)


# ---------------------------------------------------------------- 文本特征

def cl(s: str) -> int:
    """汉字个数（去掉标点与空白）。"""
    return len(re.sub(r"[^\u4e00-\u9fff]", "", s))


def feat(text: str):
    """按句读切分，返回 (三/四/五/七言占比, 分句数, 最长分句字数)。"""
    cs = [c for c in CL.split(text) if cl(c)]
    n = len(cs)
    if not n:
        return {3: 0.0, 4: 0.0, 5: 0.0, 7: 0.0}, 0, 0
    lens = [cl(c) for c in cs]
    return {k: sum(1 for x in lens if x == k) / n for k in (3, 4, 5, 7)}, n, max(lens)


def poemish(text: str) -> bool:
    """像不像“齐言诗”：以五言或七言为主，或四言/三言成章。"""
    sh, n, mx = feat(text)
    return n >= 2 and mx <= 12 and (
        sh[5] >= 0.6 or sh[7] >= 0.6 or (sh[4] >= 0.75 and n >= 4) or (sh[3] >= 0.6 and n >= 4))


def clean(t: str | None) -> str:
    return re.sub(r"\s+", " ", (t or "").strip()).replace("《", "").replace("》", "")


def base(t: str) -> str:
    """标题归一：去掉尾部括号注、“N首”“·其N”，便于词牌/组诗匹配。"""
    t = re.sub(r"[（(][^）)]*[）)]$", "", t)
    for _ in range(2):
        t = re.sub(r"([一二三四五六七八九十百零〇\d]+)首$", "", t)
        t = re.sub(r"[·・]其[一二三四五六七八九十百零〇\d]+$", "", t)
        t = re.sub(r"\s*其[一二三四五六七八九十百零〇\d]+$", "", t)
    return t.strip()


def tokens(t: str) -> list[str]:
    return [x for x in re.split(r"[·・ 　（(]", t) if x]


def match(t: str, names: set[str]) -> str | None:
    """标题命中词牌/曲牌：完全相等、前缀+分隔符、首 token、或“其N·词牌”式尾 token。"""
    b = base(t)
    for n in sorted(names, key=len, reverse=True):
        if b == n:
            return n
        if b.startswith(n) and len(b) > len(n) and b[len(n)] in "·・• 　（(-—:：":
            return n
    toks = tokens(b)
    if toks and toks[0] in names:
        return toks[0]
    if len(toks) >= 2 and toks[-1] in names and re.search(r"(其[一二三四五六七八九十]|阕|首|调)$", toks[-2]):
        return toks[-1]
    return None


def match_ci_concat(t: str) -> str | None:
    """词牌与题目连写、没有分隔符的情况：《江梅引乙酉残腊感怀…》。"""
    b = base(t)
    for n in sorted(PAI, key=len, reverse=True):
        if len(n) >= 3 and b.startswith(n) and len(b) > len(n):
            return n
    return None


def mid_pai(b: str) -> str | None:
    """题名中间夹词牌：《观婺剧杂咏十三章 其五 调笑令·李大打更》。"""
    for m in re.finditer(r"(?:^|[·・\s　])([^·・\s　]{2,8})[·・]", b):
        if m.group(1) in PAI:
            return m.group(1)
    return None


def strip_preface(r: dict) -> str | None:
    """首段是长小序、其后是齐言诗 —— 返回诗正文。"""
    ps = [p for p in (r.get("paragraphs") or []) if p.strip()]
    if len(ps) < 3:
        return None
    head, rest = "".join(ps[:1]), "".join(ps[1:])
    if cl(head) < 20:
        return None
    rh, _, _ = feat(rest)
    hh, _, _ = feat(head)
    # 序多为散句或四言，诗主体是五/七言 —— 只按 5/7 言比，别让四言序把自己也算成诗
    if (rh[5] >= 0.6 or rh[7] >= 0.6) and (hh[5] < 0.6 and hh[7] < 0.6):
        return rest
    return None


# ---------------------------------------------------------------- 分类主函数

def classify(folder: str, r: dict) -> str:
    """返回体裁中文名（诗/词/曲/赋/骚/文/新诗/联）。判定顺序即优先级。"""
    t = clean(r.get("title"))
    b = base(t)
    paras = r.get("paragraphs") or []
    body = "".join(paras)
    L = cl(body)
    sh, n, mx = feat(body)
    ps = poemish(body)
    prose = len(PROSE.findall(body)) / max(1, L)
    qi = max(sh[3], sh[4], sh[5], sh[7])
    uniform = qi >= 0.8

    # 1 篇名以“辞”结尾且多“兮” → 骚（《归去来兮辞》不能被“归去来”词牌带走）
    if b.endswith("辞") and body.count("兮") >= 3 and not ps:
        return "骚"
    # 2 新诗（自由体）：白话密度高、分行多、无“兮/曰”、非齐言
    if folder == "近现代" and L >= 40 and len([x for x in paras if x.strip()]) >= 10 and not ps:
        bd = len(BAI.findall(body)) / max(1, L)
        if bd >= 0.04 and "兮" not in body and "曰" not in body and (sh[5] + sh[7]) < 0.5:
            return "新诗"
    # 3 词（词牌/曲牌两栖的调，按宋词里的使用频次定归属：常用作词牌→词，否则→曲）
    p_ci = match(t, PAI) or match_ci_concat(t) or mid_pai(b)
    if p_ci:
        rare = (p_ci in RARE) and (b == p_ci) and (sh[5] >= 0.9 or sh[7] >= 0.9) and n in (4, 8)
        if folder in QU_FOLDERS and p_ci in QU and CI_CNT.get(p_ci, 0) < 5:
            return "曲"
        if folder in TANGQIAN:
            if n >= 2 and not uniform and not rare:
                return "词"
        elif not rare:
            return "词"
    # 4 标题以“诗”“辞”收尾 → 诗（含骚体诗《寡妇诗》，含乐府歌辞《白纻辞》）
    if b.endswith("诗") or b.endswith("辞"):
        return "诗"
    # 5 曲
    if folder in QU_FOLDERS:
        if re.match(r"^【[^】]+】", t) or re.match(
                r"^(双调|中吕|南吕|正宫|仙吕|越调|商调|黄钟|大石调|般涉调)[·・]", t):
            return "曲"
        if match(t, QU):
            return "曲"
        if any(tok in QU for tok in tokens(b)):
            return "曲"
        if re.search(r"(套数|楔子|第[一二三四五六七八九十]+折)", t):
            return "曲"
        if QU_MARK.search(body[:20]):   # 曲牌标记须在篇首，避免正文引文误触
            return "曲"
    # 6 赋（“赋得/赋此/归赋…”是动词，排除）
    if (re.search(r"赋(并序)?$", b) and "赋得" not in b and not FU_EXCLUDE.search(b)
            and L >= 60):
        return "赋"
    # 7 骚
    if any(b == p or b.startswith(p + "·") or b.startswith(p + " ")
           or b.startswith(p + "・") for p in SAO_NAMES):
        return "骚"
    if body.count("兮") >= 4 and body.count("兮") / max(1, L) >= 0.02 and not ps \
            and not WEN.search(b):
        return "骚"
    # 8 联语
    if re.search(r"(对联|长联|楹联|联语)$", b):
        return "联"
    # 9 标题开头的体裁强标记（六言诗·…）
    if SHI_MARK.match(b):
        return "诗"
    # 10 诗体尾字与记事型题尾
    if SHI_TAIL.search(b) or POEM_TAIL.search(b):
        return "诗"
    # 11 文体尾字：齐言五/七言才翻成诗（《霹雳引》《箜篌引》），否则判文
    b0 = re.split(r"[·・]", b)[0]
    if WEN.search(b) or WEN.search(b0):
        if n >= 4 and ((sh[5] + sh[7]) >= 0.8 or (sh[3] >= 0.6 and n >= 8)
                       or (b.endswith("引") and ps)):
            return "诗"
        return "文"
    # 12 文言叙事标记（《口技》《秋灯琐忆》）
    if L >= 200 and len(PROSE_MARK.findall(body)) >= 3 and qi < 0.5:
        return "文"
    # 13 齐短句成章（《无根树》《劫灰》）
    if n >= 10 and mx <= 8 and qi >= 0.85:
        return "诗"
    # 14 小序 + 诗（《有会而作》《赠白马王彪·并序》）
    if strip_preface(r):
        return "诗"
    # 15 结构判文：长句、文言虚词密、诗行占比低
    if not ps:
        if L >= 60 and (prose >= 0.03 or mx >= 45):
            return "文"
        if L >= 800 and (sh[3] + sh[5] + sh[7]) < 0.5:
            return "文"
        if L >= 150 and (sh[3] + sh[5] + sh[7]) < 0.35 and prose >= 0.015:
            return "文"
    # 16 兜底
    return "诗"


# ---------------------------------------------------------------- 文本级外科编辑

def find_records(text: str) -> list[tuple[int, int]]:
    """返回顶层数组里每个对象的 (start, end) 字符区间，end 指向该对象的 '}'。"""
    spans: list[tuple[int, int]] = []
    depth = 0
    instr = False
    esc = False
    start = -1
    for i, c in enumerate(text):
        if instr:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                instr = False
            continue
        if c == '"':
            instr = True
        elif c in "[{":
            depth += 1
            if depth == 2 and c == "{":
                start = i
        elif c in "]}":
            if depth == 2 and c == "}":
                spans.append((start, i))
            depth -= 1
    return spans


def key_indent(text: str, start: int, end: int) -> str:
    """取该记录里第一行键名的缩进，作为新增字段的缩进。"""
    nl = text.find("\n", start, end)
    if nl < 0:
        return ""
    j = nl + 1
    while j < end and text[j] in " \t":
        j += 1
    return text[nl + 1:j]


def annotate_text(text: str, rows: list[dict], genres: list[int]) -> str:
    """给每条记录补/改 genreId，返回新文本。只增一行、只改一个值，不动其它字节。"""
    assert len(rows) == len(genres)
    spans = find_records(text)
    if len(spans) != len(rows):
        raise RuntimeError(f"记录数对不上：文本里 {len(spans)} 个对象，数据里 {len(rows)} 条")
    nl = "\r\n" if "\r\n" in text[:4096] else "\n"
    out = text
    for (s, e), rec, gid in reversed(list(zip(spans, rows, genres))):
        prefix = key_indent(out, s, e)
        fixed = set_genre_id(out[s:e + 1], gid, prefix, nl)
        if fixed != out[s:e + 1]:
            out = out[:s] + fixed + out[e + 1:]
    return out


# 老版本（v1）写进去的 "genre" 名称字段：尾部形态（前面是逗号）与中间形态各一条。
# 两种都要能吃，否则「先去掉名称、再补 id」的顺序一变就会留下孤零零的逗号。
_GENRE_TAIL = re.compile(r',[ \t]*\r?\n[ \t]*"genre"[ \t]*:[ \t]*"[^"]*"')
_GENRE_MID = re.compile(r'"genre"[ \t]*:[ \t]*"[^"]*"[ \t]*,[ \t]*\r?\n[ \t]*')
_GENRE_ID = re.compile(r'"genreId"[ \t]*:[ \t]*\d+')


def set_genre_id(rec_text: str, gid: int, indent: str, nl: str) -> str:
    """对**单条记录**的原文做两件事：摘掉旧的 "genre" 名称、写入/更新 genreId。

    why 要保留原来那行 genreId 的位置而不是删了重插：重复跑 --apply 时文本必须
    逐字节不变（幂等），否则每次重新标注都会把整个语料再改一遍、diff 一片红。
    """
    t = _GENRE_MID.sub("", rec_text)
    t = _GENRE_TAIL.sub("", t)
    m = _GENRE_ID.search(t)
    if m:
        return t[:m.start()] + ('"genreId": %d' % gid) + t[m.end():]
    k = len(t) - 1
    if t[k] != "}":
        raise RuntimeError("记录结尾不是 '}'，无法插入 genreId")
    k -= 1
    while k > 0 and t[k] in " \t\r\n":      # 退到最后一个非空字符（上一个字段的收尾）
        k -= 1
    # 逗号必须紧跟上一个字段，再换行缩进写 genreId：原来 `}\n ]` 那截空白原样保留，
    # 所以文件里只会多出一行，换行符/缩进一个字节都不改。
    return t[:k + 1] + (',%s%s"genreId": %d' % (nl, indent, gid)) + t[k + 1:]


def load_folder(folder: str) -> list[tuple[str, str, list[dict]]]:
    """返回 [(路径, 原文, 记录列表), …]。"""
    out = []
    for f in sorted(glob.glob(os.path.join(ROOT, folder, "poet.*.json"))):
        # newline="" 关掉通用换行：文件里是 CRLF 就得原样读进来，否则回写时会被统一成 LF，
        # 整文件重排、diff 爆炸（八个集里只有 近现代 是 LF，其余都是 CRLF）。
        with open(f, encoding="utf-8-sig", newline="") as fh:
            text = fh.read()
        out.append((f, text, json.loads(text)))
    return out


# ---------------------------------------------------------------- 三个动作

def cmd_dry(samples: int, only: str | None) -> None:
    _tables()
    total = collections.Counter()
    for folder in FOLDERS:
        if only and folder != only:
            continue
        rows = [r for _, _, recs in load_folder(folder) for r in recs]
        c = collections.Counter()
        eg: dict[str, list[dict]] = collections.defaultdict(list)
        for r in rows:
            g = classify(folder, r)
            c[g] += 1
            eg[g].append(r)
        total.update(c)
        stat = "、".join(f"{k} {v}" for k, v in sorted(c.items(), key=lambda x: BY_NAME[x[0]][0]))
        print(f"{folder:<4} {len(rows):>6} 条  {stat}")
        if samples:
            for g, recs in sorted(eg.items(), key=lambda x: BY_NAME[x[0]][0]):
                pick = random.sample(recs, min(samples, len(recs)))
                print(f"     {g}：" + "；".join(f"{p['author']}《{p.get('title')}》" for p in pick))
    print("-" * 60)
    stat = "、".join(f"{k} {v}" for k, v in sorted(total.items(), key=lambda x: BY_NAME[x[0]][0]))
    print(f"合计 {sum(total.values())} 条：{stat}")


def cmd_apply(only: str | None) -> None:
    _tables()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    tgz = os.path.join(BACKUP_DIR, f"genres-before-{stamp}.tar.gz")
    folders = [only] if only else FOLDERS
    with tarfile.open(tgz, "w:gz") as tar:
        for folder in folders:
            tar.add(os.path.join(ROOT, folder), arcname=folder)
    print(f"备份：{os.path.relpath(tgz, ROOT)}")

    write_registry()
    changed = 0
    for folder in folders:
        for path, text, rows in load_folder(folder):
            genres = [BY_NAME[classify(folder, r)][0] for r in rows]
            new = annotate_text(text, rows, genres)
            _self_check(text, new, rows, genres, path)
            if new != text:
                with open(path, "w", encoding="utf-8", newline="") as fh:
                    fh.write(new)
                changed += 1
                c = collections.Counter(BY_ID[g][2] for g in genres)
                stat = "、".join(f"{k} {v}" for k, v in sorted(c.items(), key=lambda x: BY_NAME[x[0]][0]))
                print(f"  写入 {os.path.relpath(path, ROOT):<40} {len(rows):>6} 条  {stat}")
            else:
                print(f"  跳过 {os.path.relpath(path, ROOT):<40}（已标注）")
    print(f"完成：{changed} 个文件有改动；登记表 {os.path.relpath(REGISTRY, ROOT)}")


def _self_check(old: str, new: str, rows: list[dict], genres: list[int], path: str) -> None:
    """写盘前的硬校验：JSON 合法、条数不变、原字段（除 genreId/genre）逐字节未动。"""
    got = json.loads(new)
    assert len(got) == len(rows), f"{path}: 条数变了 {len(rows)} → {len(got)}"
    for a, b, gid in zip(rows, got, genres):
        for k, v in a.items():
            if k in ("genreId", "genre"):
                continue          # 允许按新规则重算（`--apply` 就是用来重标的）
            assert b.get(k) == v, f"{path}: 字段 {k} 被改动"
        assert set(b) - set(a) - {"genreId"} == set(), \
            f"{path}: 多出意外字段 {set(b) - set(a) - {'genreId'}}"
        assert "genre" not in b, f"{path}: 残留 genre 名称字段（v3.5 起只留 genreId）"
        assert b["genreId"] == gid, f"{path}: 体裁 id 不一致"
        assert list(b)[-1] == "genreId", f"{path}: genreId 必须是最后一个字段"


def write_registry() -> None:
    """体裁登记表：唯一 ID 的权威出处，供检索端与构建端共用。"""
    per_folder: dict[str, dict[str, int]] = {}
    for folder in FOLDERS:
        c = collections.Counter()
        for _, _, rows in load_folder(folder):
            for r in rows:
                c[classify(folder, r)] += 1
        per_folder[folder] = dict(c)
    doc = {
        "_about": "诗词语料体裁登记表。id 为唯一、稳定的体裁标识，跨文件夹共用；"
                  "源数据每条只带 genreId（不带名称），名称以本表与建库后 SQLite 的 "
                  "genre 表为准。定义在 poetry-pipeline/schema.py 的 GENRES，"
                  "由 poetry-pipeline/genre_tag.py 生成。",
        "version": 2,
        "genres": [
            {"id": i, "code": code, "name": name, "desc": desc}
            for i, code, name, desc in GENRES
        ],
        "folders": per_folder,
        "counts": {
            name: sum(v.get(name, 0) for v in per_folder.values())
            for _, _, name, _ in GENRES
        },
        "total": sum(sum(v.values()) for v in per_folder.values()),
    }
    with open(REGISTRY, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def cmd_verify(only: str | None) -> None:
    _tables()
    bad = 0
    total = 0
    for folder in ([only] if only else FOLDERS):
        c = collections.Counter()
        for path, text, rows in load_folder(folder):
            spans = find_records(text)
            if len(spans) != len(rows):
                print(f"[FAIL] {path}: 文本对象数与数据条数不符")
                bad += 1
                continue
            for r in rows:
                total += 1
                gid = r.get("genreId")
                if gid not in BY_ID:
                    print(f"[FAIL] {path}: 缺 genreId 或值越界 -> {r.get('title')}")
                    bad += 1
                    continue
                if "genre" in r:
                    # 名称是字典表的数据，不进源文件：一份事实存两处必然分叉
                    print(f"[FAIL] {path}: 残留 genre 名称字段 -> {r.get('title')}")
                    bad += 1
                c[BY_ID[gid][2]] += 1
        stat = "、".join(f"{k} {v}" for k, v in sorted(c.items(), key=lambda x: BY_NAME[x[0]][0]))
        print(f"{folder:<4} {sum(c.values()):>6} 条  {stat}")
    print(f"{'OK' if not bad else 'FAIL'}：校验 {total} 条，问题 {bad} 处")
    sys.exit(1 if bad else 0)


def main() -> None:
    ap = argparse.ArgumentParser(description="给八个朝代文件夹的每条记录标注体裁 ID")
    ap.add_argument("--apply", action="store_true", help="备份后原地写入（默认只试标）")
    ap.add_argument("--verify", action="store_true", help="校验已写入的标注")
    ap.add_argument("--folder", help="只处理某一集，如 金")
    ap.add_argument("--samples", type=int, default=0, help="试标时每类抽几条看")
    args = ap.parse_args()
    if args.folder and args.folder not in FOLDERS:
        ap.error(f"--folder 只能是 {FOLDERS}")
    if args.verify:
        cmd_verify(args.folder)
    elif args.apply:
        cmd_apply(args.folder)
    else:
        cmd_dry(args.samples, args.folder)


if __name__ == "__main__":
    main()
