# -*- coding: utf-8 -*-
"""
reconcile_gui.py —— 诗词库差异校对图形界面（打包为 .exe 使用）

和命令行的 `crosscheck.py reconcile` 做同一件事，但把所有判断交给人：
  1) 打开一份 diff 报告（默认 xcheck/ 里最新一份 diff-*.csv）；
  2) 表格分页展示每条差异，点开一条可在右侧编辑「作者 / 标题」的取值
     （保持原值 / 采用网站值 / 自定义）；
  3) 点「保存修改」把决定写回源 JSON（字节级最小改写，备份在 xcheck/patch-backup/），
     并导出 reconciled-*.csv（剩未消差异）+ eliminated-*.csv（已消除留存）；
     **落了盘的行同时从报告文件里就地删掉**（原样抄进 已处理-<时间戳>.csv，报告原文副本
     进 xcheck/patch-backup/<时间戳>/）——报告是待办清单，办完的行留在里面，下次加载
     又会原样冒出来，翻到最后也不知道哪些是真没办；
  4) 选中一条时，右侧上下并排显示「源 JSON 解析出的标题/作者/正文」与「网上
     （chagushici.com）的标题/作者/正文」，两块同版式（`标题：`/`作者：` 打头），
     省得为了看一眼正文就跳到浏览器；原始 JSON 点「完整 JSON」另开窗看：
     网上内容优先读 xcheck/html/ 缓存（与 crosscheck.py 共用同一份缓存），
     没有才联网抓详情页并落盘；联网可随时用「联网」勾选框关掉，关掉后退到 ref.db
     的列表页预览（会明确标注「可能被截断」——它是列表页预览，长诗会被截断）。
     两边标题都先过 bare_title 剥掉外层《》再比——详情页 h1 里带书名号、本地 JSON
     不带，不剥就永远显示「标题不一致」。

工程目录（含 sources.py / xcheck/）在启动时自动探测：优先读同目录或上级里的
sources.py，再沿父目录找含 全唐诗/ 或 strains/ 的数据根；都找不到就弹窗让用户选。
运行时会把该目录加进 sys.path 再 import sources，只借它的 COLLECTIONS / glob_shards /
ROOT / load_json / get_body / pick_title 做路径解析与正文取值（这样面板里的正文与
算相似度用的是同一段文本）——其余逻辑（clean / 相似度 / 作者归一 / 详情页抓取与解析）
都在本文件内，不依赖任何第三方库（zhconv 可选，没有就跳过繁转简，不影响比对）。
「写回源 JSON」那段字节级最小改写抽到了同目录的 patchjson.py，与 app.py 共用一份实现
（改语料这件事全工程只能有一套校验、一套备份），打包时两个文件都要带上。
"""

from __future__ import annotations
import os
import re
import sys
import json
import csv
import shutil
import queue
import threading
import time
import traceback
from collections import defaultdict, Counter, OrderedDict

try:
    import patchjson                       # 同目录的姊妹模块：语料 JSON 的字节级最小改写
except ImportError:                        # 被从别处 import 时同目录可能不在 sys.path
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import patchjson

# ---- 仅在打包后失败时把异常落盘，方便排查（窗口模式看不到控制台）
# why 要分 frozen：--onefile 打包后 __file__ 指向 _MEIPASS 临时目录，退出即删，
# 日志写进去等于没写。frozen 时落到 exe 旁边。
if getattr(sys, "frozen", False):
    LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "reconcile_gui.log")
else:
    LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reconcile_gui.log")


def _log_exc(msg: str):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except Exception:
        pass


# ============================================================ 工程目录探测
def find_project_root(start: str) -> str:
    """从 start 向上找含 sources.py 的目录（即 poetry-pipeline）。"""
    cur = os.path.abspath(start)
    for _ in range(8):
        if os.path.isfile(os.path.join(cur, "sources.py")) and os.path.isdir(os.path.join(cur, "xcheck")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return ""


def find_data_root(start: str) -> str:
    """从 start 向上找源数据根（含 全唐诗/ 或 strains/ 或 rank/）。"""
    cur = os.path.abspath(start)
    for _ in range(8):
        for probe in ("全唐诗", "宋词", "strains", "rank"):
            if os.path.isdir(os.path.join(cur, probe)):
                return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return ""


def resolve_project_root(exe_dir: str) -> str:
    cfg = os.path.join(exe_dir, ".reconcile_root.txt")
    if os.path.isfile(cfg):
        try:
            p = open(cfg, encoding="utf-8").read().strip()
            if p and os.path.isdir(p) and os.path.isfile(os.path.join(p, "sources.py")):
                return p
        except Exception:
            pass
    root = find_project_root(exe_dir)
    return root


# ============================================================ 纯函数（自包含）
ANON = {"佚名", "无名氏", "无名", "不详", "未知", "anonymous", "失名"}
REL_MARK = ("妻", "妾", "姬", "母", "女", "妹", "姊", "姐", "父", "子", "兄", "弟", "伶", "婢")
PUNCT = re.compile(r"[，。！？；：、,.;:!?（）()「」《》〈〉\[\]\s\"'·・…—－\-]")

# 同人异名表（与 crosscheck.py 的 ALIAS 保持一致）：庙号/字号/异写归一，
# 这样「太宗皇帝 → 李世民」这类会被判为同一人、默认采用网站值。
ALIAS = {
    "太宗皇帝": "李世民", "高宗皇帝": "李治", "中宗皇帝": "李显", "睿宗皇帝": "李旦",
    "明皇帝": "李隆基", "玄宗皇帝": "李隆基", "唐玄宗": "李隆基",
    "肃宗皇帝": "李亨", "代宗皇帝": "李豫", "德宗皇帝": "李适", "顺宗皇帝": "李诵",
    "宪宗皇帝": "李纯", "穆宗皇帝": "李恒", "敬宗皇帝": "李湛", "文宗皇帝": "李昂",
    "武宗皇帝": "李炎", "宣宗皇帝": "李忱", "懿宗皇帝": "李漼", "僖宗皇帝": "李儇",
    "昭宗皇帝": "李晔",
    "则天皇后": "武则天", "武后": "武则天", "天后": "武则天", "则天": "武则天",
    "徐贤妃": "徐惠", "上官昭容": "上官婉儿", "梅妃": "江采萍", "杨贵妃": "杨玉环",
    "后主煜": "李煜", "李后主": "李煜", "南唐后主": "李煜",
    "宋太祖": "赵匡胤", "宋太宗": "赵炅", "宋真宗": "赵恒", "宋仁宗": "赵祯",
    "宋神宗": "赵顼", "宋徽宗": "赵佶", "宋高宗": "赵构", "宋孝宗": "赵昚",
    "清昼": "皎然",
    "苏东坡": "苏轼", "李太白": "李白",
    "杜少陵": "杜甫", "杜工部": "杜甫", "少陵": "杜甫",
    "辛稼轩": "辛弃疾", "稼轩": "辛弃疾", "易安居士": "李清照",
    "陶渊明": "陶潜", "陶元亮": "陶潜", "纳兰容若": "纳兰性德",
}


def clean(s: str) -> str:
    return PUNCT.sub("", s or "")


def similarity(a: str, b: str) -> float:
    import difflib
    return round(difflib.SequenceMatcher(None, a, b).ratio(), 3)


def bare_title(s: str) -> str:
    """剥掉标题外层的书名号。详情页 <h1 class="maintitle"> 里标题天生带《》，
    本地 JSON 里的标题是裸的，不剥两边就永远看着不一样。只剥**成对的外层**，
    不动标题内部的《》（如「见道边死人（一作…《统签》并入…）」那种校勘夹注）。"""
    t = (s or "").strip()
    while len(t) >= 2 and t[0] == "《" and t[-1] == "》":
        t = t[1:-1].strip()
    return t


def format_side_parts(title, author, lines):
    """format_side 的两截：头部行 + 正文行。"""
    head = []
    if title:
        head.append("标题：%s" % bare_title(title))
    if author:
        head.append("作者：%s" % author)
    return head, [str(x) for x in (lines or [])]


def format_side(title, author, lines) -> str:
    """把一边的标题/作者/正文排成同一个版式，左右两块叠着看才能逐行对。
    why 必须点名「标题：/作者：」而不是把两边原料直接倒出来：本地侧原来是把整条
    JSON 原样 dump，字段名（title/author/paragraphs）和网站那套不是一回事，肉眼
    根本对不齐——「对比有问题」的问题就出在这。"""
    head, body = format_side_parts(title, author, lines)
    text = "\n".join(body)
    if not head:
        return text
    return ("%s\n\n%s" % ("\n".join(head), text)) if text else "\n".join(head)


def line_marks(a_lines, b_lines):
    """逐行比对两边正文，返回 (左边该标黄的行号, 右边该标黄的行号)，行号从 0 起。

    对齐用 clean() 归一后的文本——只差标点/空格的行要能对上，不然一处标点改动会让
    后面整段错位、全篇飘黄；**但标不标黄看原始文本**，标点改过也是改过，得让人看见
    差在哪儿。（判定那边的「正文一致」也是按 clean 归一比的，两个口径不冲突：一个
    回答「算不算一致」，一个回答「字面上差在哪儿」。）

    difflib 是标准库；SequenceMatcher 拿行列表当序列正好。autojunk 必须关掉——它会把
    「出现频率高的元素」当噪声剔掉，而正文里重复的句子不少，剔掉就错位了。
    """
    import difflib
    sm = difflib.SequenceMatcher(None, [clean(x) for x in a_lines],
                                 [clean(x) for x in b_lines], autojunk=False)
    ma, mb = set(), set()
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            ma.update(range(i1, i2))
            mb.update(range(j1, j2))
            continue
        for k in range(i2 - i1):
            # 归一等、原文不等 = 只改了标点/空格；这也算差异，两边都标
            if a_lines[i1 + k] != b_lines[j1 + k]:
                ma.add(i1 + k)
                mb.add(j1 + k)
    return ma, mb


def canon_author(a: str) -> str:
    b = re.sub(r"[^\u4e00-\u9fff A-Za-z]", "", a or "").strip()
    try:
        from zhconv import convert as _c
        b = _c(b, "zh-hans")
    except Exception:
        pass
    return ALIAS.get(b, b)


def loose_eq(a: str, b: str) -> bool:
    ca, cb = clean(a), clean(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    if len(ca) >= 2 and len(cb) >= 2:
        return ca in cb or cb in ca
    return False


def _author_merge_safe(old: str, new: str) -> bool:
    if not old or not new or old == new:
        return False
    lo, hi = (old, new) if len(old) <= len(new) else (new, old)
    if lo not in hi:
        return False
    i = hi.index(lo)
    leftover = hi[:i] + hi[i + len(lo):]
    return not any(m in leftover for m in REL_MARK)


def default_decision(row: dict) -> dict:
    """和 reconcile 同样的安全默认：能判为同一人才默认采用网站值。"""
    new_a = (row.get("网站作者") or "").strip()
    new_t = (row.get("网站标题") or "").strip()
    old_a = (row.get("原作者") or "").strip()
    old_t = (row.get("原标题") or "").strip()
    verdict = row.get("判定", "")
    has_body = "正文" in verdict

    da = None
    if new_a and new_a not in ANON and clean(old_a) != clean(new_a):
        if (not old_a or old_a in ANON) or canon_author(old_a) == canon_author(new_a) \
           or loose_eq(old_a, new_a) or _author_merge_safe(old_a, new_a):
            da = new_a
    dt = None
    if new_t and clean(old_t) != clean(new_t):
        try:
            sim = float(row.get("标题相似度") or 0)
        except ValueError:
            sim = 0
        if sim >= 0.85:
            dt = new_t
    # body 恒为 None：正文不能像作者/标题那样自动判（同 recheck 的现状，正文差异一律
    # 留给人工），默认就只能是「不改」。要改的话在编辑区的正文框里自己敲，或点
    # 「采用网站值」（那里搬的是**详情页/ref.db**取到的正文，不是报告列）。
    return {"author": da, "title": dt, "body": None, "has_body": has_body}


def recheck_rows(rows, S, path_cache, shard_cache, body_ref=None):
    """按【源文件现状】复核报告里的每一行，把已经和网站一致的剔掉。

    判定与 crosscheck.py 的 cmd_recheck 逐条对齐——同样三个取法（get_body / pick_title /
    rec.get(coll.author)），同样 ANON / clean / canon_author / loose_eq / _author_merge_safe
    那套作者归并，正文差异同样只看原判定里的「+正文」。区别只有两处：不落盘（界面要的是
    「加载完直接看还真需要动手的那些行」），以及多一条「必须有网站参照」的兜底（见下）。

    为什么界面也要复核一遍：报告是某一刻的快照。之后人把某些条目改回了网站值，这些行在
    报告里仍旧写着「不一致」，翻到它们只能白看一遍。

    body_ref 是可选回调 body_ref(row, body) -> bool：返回 True 表示「拿**全文参照**（站点
    详情页）证实了这一行的正文已经一致」。传 None（默认，也是命令行 recheck 的老口径）就
    只看原判定，带正文差异的行永不剔除。传进来的回调只可能让行**少留**，不会多留——它说
    不了一致就维持原样，所以接错了也不会凭空清掉待办。

    返回 (留下的行, 剔除数, 因读不出源文件而保留的行数)。
    """
    colls = {c.name: c for c in S.COLLECTIONS}
    kept, gone, bad = [], 0, 0
    no_read = set()          # 读不出来的文件记一笔，免得同一份坏文件每行都重试一次

    for r in rows:
        verdict = (r.get("判定") or "").strip()
        if not verdict:
            # 不是差异报告（手工「打开报告」选了 diff2-*.csv 之类）：没有判定就没有
            # 「是否已一致」可言，原样留着。绝不能当成「没差异」清掉——那样整张表会被
            # 清空，看着就是「差异全没了」。
            kept.append(r)
            continue
        ref_a = (r.get("网站作者") or "").strip()
        ref_t = (r.get("网站标题") or "").strip()
        if not ref_a and not ref_t:
            # 报告里压根没有网站值可比：未收录/同名异作这两类就是这样（站内没有这首，
            # cmd_check.py 里 rt/ra 一直是空串）。没有参照就不该判「已一致」——剔掉它们
            # 等于说站内根本不存在的诗「已消除(现与网站一致)」，是句假话；而且这类行
            # 本来也不该按「不一致」处理。一律留着。
            # （cmd_recheck 没这条兜底，会把它们挪进已消除清单——这里不跟它一致。）
            kept.append(r)
            continue
        # 正文差异默认只看原判定（与 cmd_recheck 一致）。但报告里那个「正文相似度」
        # 是**建报告时**拿站点**列表页预览**算的，而列表页预览对长诗必然截断（sid=306
        # 列表页只有 4 句、详情页 10 句）——于是「源文件与站点全文其实一字不差」的行会
        # 永远挂着正文差异，翻到它、看下面比对带，两处说的不是一回事。详情页全文就缓存在
        # xcheck/html/ 里，拿它对一遍就能把这类行剔掉，见下面的 body_ref。
        body_diff = ("+正文" in verdict) or verdict == "不一致:正文"

        cur_a = cur_t = ""
        body = []
        err = ""
        cname, base = r.get("集合", ""), r.get("文件", "")
        coll = colls.get(cname)
        if not coll:
            err = "未知集合 %s" % cname
        else:
            key = (cname, base)
            path = path_cache.get(key)
            if path is None:
                path = resolve_path(S, cname, base)
                path_cache[key] = path
            if not path:
                err = "找不到源文件 %s/%s" % (cname, base)
            elif path in no_read:
                err = "读源文件失败"
            else:
                try:
                    idx = int(r.get("文件内序号", ""))
                except (TypeError, ValueError):
                    err = "文件内序号无法解析"
                else:
                    data = shard_cache.get(path)
                    if data is None:
                        try:
                            data = S.load_json(path)
                            if isinstance(data, dict):
                                data = [data]
                            if not isinstance(data, list):
                                raise ValueError("顶层不是数组")
                        except Exception as e:
                            # 失败不塞进 shard_cache：那是个共享缓存，_load_local 从里面
                            # 取出来就当 list 用（会 len()/下标），塞个非 list 进去会炸在
                            # 别处。改用本函数自己的 no_read 记账。
                            no_read.add(path)
                            data = None
                            err = "读源文件失败：%s" % e
                        else:
                            shard_cache[path] = data
                            while len(shard_cache) > 8:
                                shard_cache.popitem(last=False)
                    if not err:
                        if not 0 <= idx < len(data):
                            err = "序号 %d 越界（该文件 %d 条）" % (idx, len(data))
                        else:
                            rec = data[idx]
                            if not isinstance(rec, dict):
                                err = "该条不是对象"
                            else:
                                body = S.get_body(rec, coll)
                                cur_t = S.pick_title(rec, coll, body)
                                cur_a = (rec.get(coll.author) or "") if coll.author else ""
                                if not isinstance(cur_a, str):
                                    cur_a = ""
        # 读不出来的行一律留着：源文件没读到就判「已消除」，等于让它从工作表里无声消失，
        # 而它其实一次都没被复核过（cmd_recheck 里那句「status 必须一起判」同理）。
        if err:
            kept.append(r)
            bad += 1
            continue

        if body_diff and body_ref is not None:
            # 拿全文参照复核正文差异。只有它说 True 才动判定；拿不到全文（没缓存/被挡/
            # 链接里没有 ID）就维持原样，行照旧留着。**绝不能用 ref.db 当参照**——它是
            # 同一个列表页截断预览，实测拿它比 69819 行里 0 行相符。
            try:
                if body_ref(r, body):
                    body_diff = False
            except Exception:
                pass

        cur_a, cur_t = (cur_a or "").strip(), (cur_t or "").strip()
        author_diff = False
        if ref_a and ref_a not in ANON:
            if not cur_a:
                author_diff = True
            elif clean(cur_a) == clean(ref_a):
                author_diff = False
            elif (canon_author(cur_a) == canon_author(ref_a)
                  or loose_eq(cur_a, ref_a) or _author_merge_safe(cur_a, ref_a)):
                author_diff = False
            else:
                author_diff = True
        title_diff = False
        if ref_t and cur_t and clean(cur_t) != clean(ref_t):
            title_diff = True
        elif bool(ref_t) != bool(cur_t):
            title_diff = True

        if author_diff or title_diff or body_diff:
            kept.append(r)
        else:
            gone += 1
    return kept, gone, bad


def resolve_path(S, coll_name: str, base: str) -> str:
    colls = {c.name: c for c in S.COLLECTIONS}
    coll = colls.get(coll_name)
    if not coll:
        return ""
    for f in S.glob_shards(coll.pattern):
        if os.path.basename(f) == base:
            return f
    return ""


# ============================================================ 网上内容（详情页）
# UA / COOKIE / 缓存判据 / 重试退避都照抄 crosscheck.py（第 54-59 行与 fetch_detail，
# 第 948-970 行），这样两处抓下来的 HTML 完全一致，能共用同一个 xcheck/html/ 缓存。
# why 复制而不是 import crosscheck：本文件承诺不依赖第三方库，且打包成 --windowed
# 的 exe 后 sys.stdout 是 None —— crosscheck.py 第 44 行的 sys.stdout.reconfigure
# 会直接 AttributeError；它的 HTML_DIR 还会指向 _MEIPASS 而非真实工程目录。
BASE = "https://www.chagushici.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
# 站点 Nginx 层的浏览器校验只看这个 Cookie，不带会被挡到 JS 倒计时页
COOKIE = "safe_token=ok"
CHALLENGE = "正在验证浏览器"          # 被挡时页面前部会出现这句
TXT_CAP = 40000                       # 面板里单块文本上限（古文观止单条 16 万字）
# 正文编辑框能承载的上限。与面板同一档：比这更长就把编辑关掉（不是截断后再写回——把
# 截断过的正文写进源文件，等于静默删字）。真要改这么大的条目请走源文件或拆分。
BODY_EDIT_CAP = TXT_CAP


def strip_tags(s: str) -> str:
    import html
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def parse_detail_html(raw: bytes) -> dict:
    """详情页 HTML → {"title", "author", "lines"}；解析不出东西就返回 {}。

    抽查 3000 个 xcheck/html/shi-*.html：全部含 class="shici-text" 与
    class="maintitle"，且正文块内 <div> 数为 0 —— 所以非贪婪正则可直接用，
    不需要 crosscheck 那套 <div> 配对深度扫描（_blocks，crosscheck.py:888）。
    """
    txt = raw.decode("utf-8", errors="replace")
    if CHALLENGE in txt[:4000]:
        return {}
    m = re.search(r'<div class="shici-text">(.*?)</div>', txt, re.S)
    if not m:
        return {}
    body = strip_tags(re.sub(r"<br\s*/?>", "\n", m.group(1)))
    lines = [x.strip() for x in body.split("\n") if x.strip()]

    # 标题与作者同在一行：<h1 class="maintitle ...">《饮马长城窟行》 李世民</h1>
    title = author = ""
    h1 = re.search(r'<h1[^>]*class="maintitle[^"]*"[^>]*>(.*?)</h1>', txt, re.S)
    if h1:
        s = strip_tags(h1.group(1))
        mt = re.match(r"^《(.*?)》\s*(.*)$", s, re.S)
        if mt:
            title, author = mt.group(1).strip(), mt.group(2).strip()
        else:
            title = s
    if not lines and not title:
        return {}
    return {"title": title, "author": author, "lines": lines}


def read_cached_detail(html_dir: str, sid: int) -> bytes:
    """只读缓存。有效性判据同 crosscheck.fetch_detail：>3000 字节且没被挡。"""
    try:
        d = open(os.path.join(html_dir, "shi-%d.html" % sid), "rb").read()
    except OSError:
        return b""
    if len(d) > 3000 and CHALLENGE.encode("utf-8") not in d[:4000]:
        return d
    return b""


def fetch_detail_html(sid: int, html_dir: str, sleep: float = 0.25) -> bytes:
    """抓详情页并落缓存（缓存有效则不联网）。语义同 crosscheck.fetch_detail。"""
    import time
    import urllib.request

    d = read_cached_detail(html_dir, sid)
    if d:
        return d
    try:
        os.makedirs(html_dir, exist_ok=True)
    except OSError:
        return b""
    url = "%s/shici/%d" % (BASE, sid)
    data = b""
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Cookie": COOKIE,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept-Encoding": "identity",
                "Referer": BASE + "/",
            })
            with urllib.request.urlopen(req, timeout=25) as r:
                data = r.read()
            if len(data) < 3000:
                raise RuntimeError("页面过小")
            break
        except Exception:
            data = b""
            time.sleep(1.5 * (attempt + 1))
    if data:
        # tmp 名带 pid：crosscheck.py 正好在跑时，两边会抢同一个 .tmp
        tmp = os.path.join(html_dir, "shi-%d.html.tmp-%d" % (sid, os.getpid()))
        open(tmp, "wb").write(data)
        os.replace(tmp, os.path.join(html_dir, "shi-%d.html" % sid))
        time.sleep(sleep)             # 礼貌抓取，避免连着点十几条把站点打满
    return data


def ref_lines(db_path: str, sid: int) -> dict:
    """兜底：拿 ref.db 里的列表页预览。长诗会被截断（sid=306 只有 4 句，
    详情页有 10 句），所以只能当参考，界面上必须标注出来。"""
    try:
        import sqlite3
        con = sqlite3.connect(db_path)
        try:
            row = con.execute(
                "SELECT title, author, lines FROM ref WHERE sid=?", (sid,)).fetchone()
        finally:
            con.close()
    except Exception:
        return {}
    if not row:
        return {}
    try:
        lines = json.loads(row[2]) if row[2] else []
    except Exception:
        lines = []
    if not isinstance(lines, list):
        lines = []
    return {"title": (row[0] or "").strip(), "author": (row[1] or "").strip(),
            "lines": [str(x) for x in lines]}


def link_sid(url_or_row) -> int:
    """/shici/123456 → 123456；给一整个报告行也行。认不出就 0。"""
    u = url_or_row.get("网站链接", "") if isinstance(url_or_row, dict) else url_or_row
    m = re.search(r"/shici/(\d+)", u or "")
    return int(m.group(1)) if m else 0


def detail_body_same(html_dir: str, sid: int, body):
    """拿 xcheck/html 里的**详情页全文**复核本地正文：
    True=已一致 / False=不一致 / None=没有可用的全文参照（没缓存、被挡、没有 ID）。

    只认详情页。ref.db 是同一个列表页的**截断预览**（长诗只留开头几句），拿它当参照会把
    完整的行判成不一致——实测：报告里 69819 条带正文差异的行，用 ref.db 比 0 条相符，
    用详情页全文比有 1926 条其实一字不差。所以这里绝不能退到 ref_lines。

    crosscheck.py 里有一份**逐字相同**的副本（cmd_recheck 的 --body-check 用它）；
    改这里记得一起改——两边的复核结论必须是同一个。
    """
    if not sid or not body:
        return None
    raw = read_cached_detail(html_dir, sid)
    if not raw:
        return None
    ls = (parse_detail_html(raw) or {}).get("lines") or []
    if not ls:
        return None
    return clean("".join(str(x) for x in body)) == clean("".join(str(x) for x in ls))


# ============================================================ GUI
def main():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext

    exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    root_dir = resolve_project_root(exe_dir)
    if not root_dir:
        # 兜底：让用户选
        _tmp = tk.Tk()
        _tmp.withdraw()
        sel = filedialog.askdirectory(title="选择 poetry-pipeline 工程目录（含 sources.py / xcheck）")
        _tmp.destroy()
        if not sel:
            messagebox.showerror("无法启动", "找不到工程目录，已退出。")
            return
        root_dir = sel
        try:
            open(os.path.join(exe_dir, ".reconcile_root.txt"), "w", encoding="utf-8").write(root_dir)
        except Exception:
            pass

    if root_dir not in sys.path:
        sys.path.insert(0, root_dir)
    import sources as S

    # 强制数据根，覆盖 sources 里硬编码的旧路径
    data_root = find_data_root(root_dir)
    if data_root:
        os.environ["POETRY_SRC"] = data_root
        # 重新取一次 ROOT（sources 模块级已算过，这里直接覆盖其全局）
        try:
            S.ROOT = data_root
        except Exception:
            pass

    WORK = os.path.join(root_dir, "xcheck")
    BACKUP = os.path.join(WORK, "patch-backup")

    app = ReconcileApp(tk, ttk, filedialog, messagebox, scrolledtext,
                       root_dir=root_dir, S=S, WORK=WORK, BACKUP=BACKUP)
    app.run()


class ReconcileApp:
    COLS = ["集合", "文件", "序号", "原标题", "原作者", "网站标题", "网站作者", "判定"]
    PAGE = 300

    def __init__(self, tk, ttk, filedialog, messagebox, scrolledtext,
                 root_dir, S, WORK, BACKUP):
        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.scrolledtext = scrolledtext
        self.root_dir = root_dir
        self.S = S
        self.WORK = WORK
        self.BACKUP = BACKUP

        self.rows = []
        # (集合,文件,序号) -> {"author":v|None,"title":v|None,"body":v|None,"has_body":bool}
        # None 一律表示「本字段不改」；body 的值是列表（按行）或字符串，形状跟着源文件走。
        self.decisions = {}
        self.csv_path = ""
        # 当前这份报告表头的**原样列序**（load_csv 里从 DictReader 抄下来）。写回后拿它重写
        # 报告、也拿它写「已处理」名单：报告的列是别人（cmd_check/cmd_recheck）定的，
        # 这里一个列都不许自作主张地加、删、换序，否则 GUI 重写的报告与命令行产出的就成了
        # 两种格式。
        self._report_fields = []
        self.filtered = []           # 当前筛选后的行索引（指向 self.rows）
        self.page = 0
        self.iid_to_idx = {}
        # 编辑区两个文本框里现在装的是**哪一条**（(集合,文件,序号) + 行）。切换选中时
        # 先用它把上一条记下来，再换成本条；选不中任何行时置 None，这样框里的残留文本
        # 不会被误记到别的行上。
        self._edit_key = None
        self._edit_row = None
        # 编辑区控件本体（_build_edit 里建）：info 一栏的 Label 和「打开网站链接」用的 url
        self.detail = {"link": ""}

        # 正文回写用：(集合,文件,序号) -> {"field","kind","ok","reason","orig"}，选中时由
        # _refresh_compare 填。field 是**实际读到正文的那个键名**（get_body 有 coll.body →
        # paragraphs → content 三级兜底，不能想当然用 coll.body）；kind 是 "str"/"list"，
        # 写回时形状要跟它一致；orig 是框里的原文，用来判断「改了没有」。
        self._meta_by_key = {}
        self._cur_meta = {}
        # (集合,文件,序号) -> {"author","title"}：作者/标题的**比对基线**，也就是这一条
        # 「不许它变」的那份现值。初始 = 报告里的快照（原作者/原标题），此后不再挪动——
        # 落过盘的行会被 _prune_applied 整行摘掉（行、判定、基线一起走），所以没有「写回
        # 成功后把基线挪到新值」这回事。判定「改了没有」一律拿框里的文本对它，不拿源文件
        # 现值——源文件可能早被上一轮写回改过，拿它当基线的话，光是选中这一行就会凭空记下
        # 一处「改动」（把快照里的旧值又写回去）。
        self._base_by_key = {}
        # (集合,文件,序号) -> 网上正文的行列表：给「采用网站值」用，也给写回后的
        # 「正文是否已与网站一致」判断用（拿不到就一律算待人工）。
        self._site_lines_by_key = {}
        self._cmp_site_lines = []
        # 集合自己声明的正文字段名（paragraphs/content/para/lines…）。写回时允许「记录里
        # 没有这个键、要新建」，但这个键名必须是集合声明过的，不能是用户敲错的名字。
        try:
            self._body_fields = set(c.body for c in self.S.COLLECTIONS if c.body)
        except Exception:
            self._body_fields = set()

        # ---- 比对区
        # 源文件读取缓存：每次选中都要 glob + 读盘太浪费（单文件可达几 MB）
        self._path_cache = {}                 # (集合, 文件) -> 路径
        self._shard_cache = OrderedDict()      # 路径 -> parsed list（超过 8 个按插入序淘汰）
        self._cmp_local = {}                   # 当前选中条目的本地侧（字典，见 _load_local）
        self._site_html_dir = os.path.join(self.WORK, "html")
        self._site_db = os.path.join(self.WORK, "ref.db")
        self._online = True                    # 纯 Python 开关：子线程不能碰 tkinter 变量
        # 网上抓取：单工作线程 + 请求令牌。子线程只做网络/磁盘/sqlite，产出丢进队列，
        # 一律由主线程渲染（同 app.py 的 queue + after 轮询写法）。
        self._siteq = queue.Queue()
        self._want = None                      # 最新一次请求 (sid, token)
        self._want_lock = threading.Lock()
        self._wake = threading.Event()
        self._token = 0
        self._inflight = False
        self._inflight_sid = None              # 正在抓的 sid，给同一条重复请求去重用

        self.root = tk.Tk()
        self.root.title("诗词库差异校对工具  ·  %s" % os.path.basename(root_dir))
        # Tk 回调里的异常默认只往 stderr 打：打包成 --windowed 的 exe 后 sys.stdout/stderr
        # 是 None，等于扔掉——界面停在旧内容上、日志里一个字都没有，看着就像「选了新数据
        # 却没更新」。装个钩子把它落到 reconcile_gui.log，并当场提示一次。
        self._exc_shown = 0
        self.root.report_callback_exception = self._on_callback_exc
        try:
            import tkinter.font as _tkfont
            self.FONT = _tkfont.Font(family="Microsoft YaHei", size=10)
            self.FONT_BOLD = _tkfont.Font(family="Microsoft YaHei", size=10, weight="bold")
            self.FONT_TITLE = _tkfont.Font(family="Microsoft YaHei", size=11, weight="bold")
            # 用命名字体对象（.name 是 Tk 内部字体名），避免裸字符串 "Microsoft YaHei 10"
            # 被 Tk 误解析成 字体名=Microsoft 字号=YaHei 而报 expected integer but got "YaHei"
            self.root.option_add("*Font", self.FONT.name)
        except Exception:
            self.FONT = self.FONT_BOLD = self.FONT_TITLE = None
        self.root.geometry("1320x900")
        self._build()
        threading.Thread(target=self._site_worker, daemon=True, name="site-fetch").start()

    # ---------------------------------------------------------- 界面
    def _build(self):
        tk, ttk = self.tk, self.ttk

        # 顶部工具栏
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=6, pady=4)
        ttk.Button(bar, text="打开报告", command=self.open_csv).pack(side="left", padx=2)
        # 「保存修改」和它的「演练(不写盘)」跟着编辑区走（见 _build_detail）——它们是
        # 同一个动作的两半，摆在编辑区才和「文本框里的值」挨着；这里只留全局动作。
        ttk.Button(bar, text="导出瘦身报告", command=self.export_only).pack(side="left", padx=2)
        ttk.Button(bar, text="本页全用网站值", command=self.page_apply_site).pack(side="left", padx=2)
        ttk.Button(bar, text="本页全跳过", command=self.page_skip).pack(side="left", padx=2)
        ttk.Button(bar, text="工程目录: " + os.path.basename(self.root_dir),
                   command=self.show_root).pack(side="right", padx=2)

        # 筛选行
        fbar = ttk.Frame(self.root)
        fbar.pack(fill="x", padx=6, pady=2)
        ttk.Label(fbar, text="判定:").pack(side="left")
        self.verdict_var = tk.StringVar(value="全部")
        self.verdict_cb = ttk.Combobox(fbar, textvariable=self.verdict_var, width=18, state="readonly")
        self.verdict_cb.pack(side="left", padx=2)
        self.verdict_cb.bind("<<ComboboxSelected>>", lambda e: self.apply_filter())
        ttk.Label(fbar, text="集合:").pack(side="left")
        self.coll_var = tk.StringVar(value="全部")
        self.coll_cb = ttk.Combobox(fbar, textvariable=self.coll_var, width=14, state="readonly")
        self.coll_cb.pack(side="left", padx=2)
        self.coll_cb.bind("<<ComboboxSelected>>", lambda e: self.apply_filter())
        ttk.Label(fbar, text="搜索:").pack(side="left")
        self.search_var = tk.StringVar()
        ttk.Entry(fbar, textvariable=self.search_var, width=22).pack(side="left", padx=2)
        ttk.Button(fbar, text="筛选", command=self.apply_filter).pack(side="left", padx=2)
        ttk.Button(fbar, text="重置", command=self.reset_filter).pack(side="left", padx=2)
        # 加载后自动按【源文件现状】复核一遍，剔掉已经和网站一致的行。默认开着：报告是
        # 快照，人改回网站值之后那些行还留在里面，不剔掉只能白翻。留开关是因为它确实会
        # 改变「报告里有多少行」这个直观印象——关掉就能看到报告原样（复核不写任何文件）。
        self.auto_recheck_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(fbar, text="自动复核剔除已一致", variable=self.auto_recheck_var,
                        command=self.reload_csv).pack(side="left", padx=6)

        # 主体分三段：上（左表 | 右编辑区）、下（本地 | 网上 并排比对带）。
        # why 比对带横着铺满整窗、不再挤在右栏里：本地正文和网上正文要「并排」着看，右栏
        # 总共才 500 来像素，并排后每边只剩十几个字一行，还没读完一行就要横向滚——那不叫
        # 并排。铺到整窗宽度后两块各有 600+ 像素，同时右栏腾出来的纵向空间正好留给新增的
        # 正文编辑框。
        outer = ttk.PanedWindow(self.root, orient="vertical")
        outer.pack(fill="both", expand=True, padx=6, pady=4)
        top = ttk.Frame(outer)
        cmpband = ttk.Frame(outer)
        outer.add(top, weight=3)
        outer.add(cmpband, weight=2)

        body = ttk.PanedWindow(top, orient="horizontal")
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=3)
        body.add(right, weight=2)

        # 左：分页表格
        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=self.COLS, show="headings", height=28)
        widths = [90, 150, 50, 150, 110, 150, 110, 150]
        for c, w in zip(self.COLS, widths):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="w")
        self.tree.tag_configure("auto", background="#e6ffe6")
        self.tree.tag_configure("manual", background="#fff4d6")
        self.tree.tag_configure("skip", background="#f0f0f0")
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        # 左：翻页
        pg = ttk.Frame(left)
        pg.pack(fill="x", pady=2)
        ttk.Button(pg, text="上一页", command=self.prev_page).pack(side="left")
        ttk.Button(pg, text="下一页", command=self.next_page).pack(side="left")
        self.page_label = ttk.Label(pg, text="第 0 / 0 页")
        self.page_label.pack(side="left", padx=8)

        self._build_edit(right)
        self._build_compare(cmpband)

        # 底部状态
        self.status = ttk.Label(self.root, text="未加载报告", anchor="w")
        self.status.pack(fill="x", padx=6, pady=3)

    def _build_compare(self, parent):
        """比对带：左「本地内容（源 JSON 解析）」| 右「网上内容」，**左右并排**。

        why 并排：这两块本来就是逐行对着看的（同一套 format_side 版式，标题/作者打头），
        上下叠着时各只有 8 行高、对一处要来回滚两次。并排后两块各自占满比对带的高度。
        """
        tk, ttk, scrolledtext = self.tk, self.ttk, self.scrolledtext
        pad = dict(padx=6, pady=3)

        ttk.Label(parent, text="— 选中条目比对 —", font=self.FONT_TITLE).pack(anchor="w", **pad)

        self.cmp_info = ttk.Label(parent, text="", justify="left", wraplength=1220)
        self.cmp_info.pack(anchor="w", **pad)

        cols = ttk.PanedWindow(parent, orient="horizontal")
        cols.pack(fill="both", expand=True)
        lframe = ttk.Frame(cols)
        sframe = ttk.Frame(cols)
        cols.add(lframe, weight=1)
        cols.add(sframe, weight=1)

        # 本地侧显示的是**解析后**的标题/作者/正文（load_json + get_body + pick_title），
        # 与上一步算相似度用的是同一段文本，也和右边「网上内容」同版式，能逐行对。
        # 原始 JSON 想看就点「完整 JSON」——它含字段名和正文以外的字段，是另一回事。
        lbar = ttk.Frame(lframe)
        lbar.pack(fill="x", **pad)
        ttk.Label(lbar, text="本地内容（源 JSON 解析）", font=self.FONT_BOLD).pack(side="left")
        self.local_full_btn = ttk.Button(lbar, text="完整 JSON", width=10,
                                         command=self._open_full_json, state="disabled")
        self.local_full_btn.pack(side="right")
        self.local_box = scrolledtext.ScrolledText(lframe, height=9, wrap="word")
        self.local_box.pack(fill="both", expand=True, **pad)

        # 网上内容
        sbar = ttk.Frame(sframe)
        sbar.pack(fill="x", **pad)
        self.site_label = ttk.Label(sbar, text="网上内容", font=self.FONT_BOLD)
        self.site_label.pack(side="left")
        self.online_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(sbar, text="联网", variable=self.online_var,
                        command=self._toggle_online).pack(side="right")
        ttk.Button(sbar, text="打开网站链接",
                   command=self.open_link).pack(side="right", padx=4)
        self.site_box = scrolledtext.ScrolledText(sframe, height=9, wrap="word")
        self.site_box.pack(fill="both", expand=True, **pad)

        for box in (self.local_box, self.site_box):
            # 正文差异行标黄。两边用同一个 tag 名，由 _render_side 按行打上去；
            # 底色比行标签浅一档——它盖的是「字」，不该把整块文本框压暗。
            box.tag_configure("diff", background="#ffe9a8")
            box.configure(state="disabled")

    def _build_edit(self, parent):
        tk, ttk, scrolledtext = self.tk, self.ttk, self.scrolledtext
        pad = dict(padx=6, pady=3)
        edit_frame = parent

        # ---------------- 右栏：编辑区 ----------------
        # 这里的两个文本框**始终可编辑**，且「框里的值 = 本条将要写回的值」：与原文相同
        # （clean 归一后）就是「本条不改」。radio 退化成「一键填值」的快捷方式，打字会
        # 自动落回「自定义」。
        # why 去掉「应用本条 / 跳过本条」：原来 radio 停在 keep/site 时会把 Entry 设成
        # readonly（敲不进字，得先拨到「自定义」），改完还得再点一次「应用本条」才生效；
        # 而报告里绝大多数行 default_decision 都是 None，于是 radio 永远停在「保持原值」、
        # 框里永远是该行的原值 —— 换行时控件状态一模一样，看着就是「这里没有变化、也没法
        # 修改」。现在改成「所见即所写 + 编辑即生效」，两个按钮自然就多余了。
        ttk.Label(edit_frame, text="— 修改作者 / 标题 / 正文 —",
                  font=self.FONT_TITLE).pack(anchor="w", **pad)

        info = ttk.Label(edit_frame, text="", justify="left", wraplength=420)
        info.pack(anchor="w", **pad)
        self.detail["info"] = info

        # ttk.Label(edit_frame, wraplength=420, justify="left",
        #           text="框里的值 = 写回源文件的值；与原文相同（或清空）则本条不改。"
        #                "回车 = 记下本条并跳到下一行。"
        #                "「保存本行修改」只落当前这一条；别处的改动点「全部写回」一起落盘。"
        #           ).pack(anchor="w", **pad)

        # 作者
        ttk.Label(edit_frame, text="作者", font=self.FONT_BOLD).pack(anchor="w", **pad)
        self.a_var = tk.StringVar(value="keep")
        af = ttk.Frame(edit_frame)
        af.pack(fill="x", **pad)
        ttk.Radiobutton(af, text="保持原值", variable=self.a_var, value="keep",
                        command=self._sync_author_entry).pack(side="left")
        ttk.Radiobutton(af, text="采用网站值", variable=self.a_var, value="site",
                        command=self._sync_author_entry).pack(side="left")
        ttk.Radiobutton(af, text="自定义", variable=self.a_var, value="custom",
                        command=self._sync_author_entry).pack(side="left")
        self.a_entry = ttk.Entry(edit_frame)
        self.a_entry.pack(fill="x", **pad)
        self.detail["author_old"] = ""
        self.detail["author_site"] = ""

        # 标题
        ttk.Label(edit_frame, text="标题", font=self.FONT_BOLD).pack(anchor="w", **pad)
        self.t_var = tk.StringVar(value="keep")
        tf = ttk.Frame(edit_frame)
        tf.pack(fill="x", **pad)
        ttk.Radiobutton(tf, text="保持原值", variable=self.t_var, value="keep",
                        command=self._sync_title_entry).pack(side="left")
        ttk.Radiobutton(tf, text="采用网站值", variable=self.t_var, value="site",
                        command=self._sync_title_entry).pack(side="left")
        ttk.Radiobutton(tf, text="自定义", variable=self.t_var, value="custom",
                        command=self._sync_title_entry).pack(side="left")
        self.t_entry = ttk.Entry(edit_frame)
        self.t_entry.pack(fill="x", **pad)
        self.detail["title_old"] = ""
        self.detail["title_site"] = ""

        # 正文。规则和上面两个框完全一样（框里的值 = 写回的值），三点不同：
        #   1. 一行一句，所以写回时按行拆成数组——形状跟着源文件走：本来是一个字符串的
        #      写回字符串，本来是数组的写回数组（见 _body_value / _load_local）。
        #   2. 「采用网站值」是把右边网上正文那一栏整段搬进来（_sync_body_box）。
        #   3. 不能安全回写的条目（正文是嵌套结构 / 太长 / 读不到本地记录）把框锁上并写
        #      明原因——宁可不给改，也不能按拍平的结果写回去毁掉结构。
        ttk.Label(edit_frame, text="正文（一行一句）", font=self.FONT_BOLD).pack(anchor="w", **pad)
        self.b_var = tk.StringVar(value="keep")
        bfr = ttk.Frame(edit_frame)
        bfr.pack(fill="x", **pad)
        ttk.Radiobutton(bfr, text="保持原值", variable=self.b_var, value="keep",
                        command=self._sync_body_box).pack(side="left")
        ttk.Radiobutton(bfr, text="采用网站值", variable=self.b_var, value="site",
                        command=self._sync_body_box).pack(side="left")
        ttk.Radiobutton(bfr, text="自定义", variable=self.b_var, value="custom",
                        command=self._sync_body_box).pack(side="left")
        self.b_box = scrolledtext.ScrolledText(edit_frame, height=8, wrap="word")
        self.b_box.pack(fill="both", expand=True, **pad)
        self.b_hint = ttk.Label(edit_frame, text="", justify="left", wraplength=420)
        self.b_hint.pack(anchor="w", **pad)

        # 打字 / 失焦 / 回车都当场把框里的值记进本条判定（不再有「应用本条」这一步）
        for e in (self.a_entry, self.t_entry):
            e.bind("<KeyRelease>", self._on_edit_key)
            e.bind("<FocusOut>", self._on_edit_key)
            e.bind("<Return>", self._on_edit_enter)
        # 正文框不绑回车跳行：正文里换行是常事，「跳到下一行」留给上面两个单行框
        self.b_box.bind("<KeyRelease>", self._on_edit_key)
        self.b_box.bind("<FocusOut>", self._on_edit_key)

        # 保存从顶部工具栏搬到这里：它和上面几个框是一件事，挨着才不会「改完不知道去哪保存」。
        # 两个按钮分工：左边只落**当前这一条**（它就长在这一条的三个框下面），右边把工作表里
        # 攒着的一次性全落——按钮上带处数，用户随时知道还剩几处没写。
        bf = ttk.Frame(edit_frame)
        bf.pack(fill="x", **pad)
        ttk.Button(bf, text="保存本行修改", command=self.do_save).pack(side="left", padx=2)
        self.save_all_btn = ttk.Button(bf, text="全部写回", command=self.do_save_all)
        self.save_all_btn.pack(side="left", padx=2)
        self.dry_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bf, text="演练(不写盘)", variable=self.dry_var).pack(side="left", padx=6)

        note = ttk.Label(edit_frame, text="说明", font=self.FONT_BOLD)
        note.pack(anchor="w", **pad)
        # 说明框不 expand：腾出来的纵向空间给正文框，正文比说明更需要地方
        self.note_box = scrolledtext.ScrolledText(edit_frame, height=4, wrap="word")
        self.note_box.pack(fill="x", **pad)

    def run(self):
        # 启动时默认加载最新 diff 报告
        latest = self._latest_diff()
        if latest:
            self.load_csv(latest)
        self.root.mainloop()

    def _on_callback_exc(self, exc, val, tb):
        """Tk 回调（<<TreeviewSelect>>/按钮/after）里没接住的异常兜底。
        日志必写；弹窗只弹前 3 次，免得某个反复触发的回调把弹窗刷成风暴。"""
        txt = "".join(traceback.format_exception(exc, val, tb))
        _log_exc("[回调异常]\n" + txt)
        try:
            self.set_status("出错了：%s —— 详情见 reconcile_gui.log" % val)
        except Exception:
            pass
        if self._exc_shown < 3:
            self._exc_shown += 1
            try:
                self.messagebox.showerror(
                    "出错了",
                    "%s: %s\n\n比对区可能没跟着刷新。详情已写入：\n%s" % (
                        exc.__name__, val, LOG_PATH))
            except Exception:
                pass


    # ---------------------------------------------------------- 数据
    def _latest_diff(self):
        if not os.path.isdir(self.WORK):
            return ""
        import glob as _glob
        cands = []
        for pat in ("diff-*.csv", "recheck-*.csv"):
            cands.extend(_glob.glob(os.path.join(self.WORK, pat)))
        # 排除 *-eliminated-*.csv：它是 recheck 的「已消除留存清单」，只剩表头时
        # 是个 152 字节的空报告。它和 recheck-diff-*.csv 同一次运行产出、在 NTFS 上
        # mtime 精确到同一微秒（实测两者都是 1790600691.830050），排序是平手，
        # 谁赢全看 glob 的返回顺序——赌赢只是碰巧。哪次赌输了，界面就会自动加载一份
        # 空报告、一条差异都不显示，看着像「差异全没了」。
        cands = [p for p in cands if "-eliminated-" not in os.path.basename(p)]
        if not cands:
            return ""
        # 按「修改时间」挑最新，而不是按文件名排序——
        # recheck 产出的 recheck-diff-*.csv 文件名比旧的 diff-*.csv 更“靠后”会误判，
        # 用 mtime 才能正确拿到刚刷新的那份。
        cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return cands[0]

    def open_csv(self):
        p = self.filedialog.askopenfilename(
            title="选择 diff 报告", initialdir=self.WORK,
            filetypes=[("CSV", "*.csv"), ("全部", "*.*")])
        if p:
            self.load_csv(p)

    def load_csv(self, path):
        # 换报告：编辑区里的残留文本属于上一份报告，先作废。它的 (集合,文件,序号) 很可能
        # 在新报告里也存在（同一批语料的同一分片），留着就会被当成对新报告某一行的判定，
        # 下一页「保存修改」就把它写进源文件了。
        self._edit_key = self._edit_row = None
        self._cur_meta = {}
        self._site_lines_by_key = {}
        self._meta_by_key = {}
        self._base_by_key = {}
        self.csv_path = path
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rd = csv.DictReader(fh)
            self.rows = list(rd)
            self._report_fields = list(rd.fieldnames or [])
        loaded = len(self.rows)
        # 加载完立刻按【源文件现状】复核，剔掉已经和网站一致的行（判定同 crosscheck.py 的
        # cmd_recheck，只是不落盘）。删的是 self.rows 本身而不是只改视图：序号、分页、
        # decisions 的键、翻页后的 apply 全靠 self.rows 对齐，留一份「看不见的行」在后面
        # 迟早错位；要看报告原样把上面那个开关一关重启一遍即可。
        cut = bad = 0
        cost = 0.0
        if self.auto_recheck_var.get() and self.rows:
            t0 = time.time()
            # body_ref：正文差异也拿**详情页全文**（xcheck/html 缓存）复核一遍。报告里那句
            # 「不一致:正文」当初是拿列表页摘要算的，长诗必然偏低，于是一批其实已经一字不差
            # 的行会一直挂在工作表里——这就是「顶部说不一致、底部又一致」。缓存里没有详情页
            # 的行照旧留着，选中它时会联网抓一次再现场判。
            self.rows, cut, bad = recheck_rows(
                self.rows, self.S, self._path_cache, self._shard_cache,
                body_ref=self._cached_body_same)
            cost = time.time() - t0
        # 默认决策
        self.decisions = {}
        for r in self.rows:
            key = (r.get("集合", ""), r.get("文件", ""), r.get("文件内序号", ""))
            self.decisions[key] = default_decision(r)
        # 筛选下拉
        verdicts = sorted({r.get("判定", "").split(":")[0] for r in self.rows})
        colls = sorted({r.get("集合", "") for r in self.rows})
        self.verdict_cb["values"] = ["全部"] + verdicts
        self.coll_cb["values"] = ["全部"] + colls
        self.verdict_var.set("全部")
        self.coll_var.set("全部")
        self.search_var.set("")
        self.filtered = list(range(len(self.rows)))
        self.page = 0
        self.refresh_tree()
        msg = "已加载 %s：%d 条差异" % (os.path.basename(path), len(self.rows))
        if cut or bad:
            extra = "，%d 条读不出源文件已保留" % bad if bad else ""
            msg += "（自动复核剔除 %d 条现已一致%s；报告共读入 %d 条，用时 %.1fs）" % (
                cut, extra, loaded, cost)
        self.set_status(msg)

    def reload_csv(self):
        """「自动复核剔除已一致」开关变了：对同一份报告重新加载一遍。
        复核只影响界面显示、不写任何文件，所以关掉开关就能看到报告原样。"""
        if self.csv_path and os.path.exists(self.csv_path):
            self.load_csv(self.csv_path)

    def apply_filter(self):
        if not self.rows:
            return
        vsel = self.verdict_var.get()
        csel = self.coll_var.get()
        q = self.search_var.get().strip().lower()
        out = []
        for i, r in enumerate(self.rows):
            if vsel != "全部" and r.get("判定", "").split(":")[0] != vsel:
                continue
            if csel != "全部" and r.get("集合", "") != csel:
                continue
            if q:
                blob = " ".join(str(r.get(c, "")) for c in
                                ("原标题", "原作者", "网站标题", "网站作者", "文件")).lower()
                if q not in blob:
                    continue
            out.append(i)
        self.filtered = out
        self.page = 0
        self.refresh_tree()
        self.set_status("筛选后 %d 条（共 %d）" % (len(out), len(self.rows)))

    def reset_filter(self):
        self.verdict_var.set("全部")
        self.coll_var.set("全部")
        self.search_var.set("")
        self.filtered = list(range(len(self.rows)))
        self.page = 0
        self.refresh_tree()
        self.set_status("已重置筛选：%d 条" % len(self.rows))

    def _page_indices(self):
        start = self.page * self.PAGE
        return self.filtered[start:start + self.PAGE]

    @staticmethod
    def _tag_of(dec):
        """行标签：三字段都有值→auto（默认建议），都空→skip，只改一部分→manual。"""
        dec = dec or {}
        vals = [dec.get(fld) for fld in ("author", "title", "body")]
        if all(v is not None for v in vals):
            return "auto"
        if all(v is None for v in vals):
            return "skip"
        return "manual"

    def _retag(self, key):
        """只改这一行的 tag。不重建表——重建会丢选中，iid 也会重排。"""
        # 判定变了，「全部写回（N 处）」上的处数也跟着变，顺手刷新
        self._update_save_buttons()
        for iid, ri in self.iid_to_idx.items():
            r = self.rows[ri]
            if (r.get("集合", ""), r.get("文件", ""), r.get("文件内序号", "")) == key:
                self.tree.item(iid, tags=(self._tag_of(self.decisions.get(key)),))
                return

    def _cached_body_same(self, r, body) -> bool:
        """recheck 的全文参照：拿 xcheck/html 里这一条的**详情页全文**对一遍正文。

        只认详情页缓存——ref.db 是列表页的截断摘要，拿它当基准会把完整的行判成不一致
        （实测：69819 条带正文差异的行，用 ref.db 比 0 条相符，用详情页全文比有 1926 条
        其实一字不差）。缓存没命中就返回 False（= 证实不了一致），行照旧留着。
        """
        return detail_body_same(self._site_html_dir, link_sid(r), body) is True

    def refresh_tree(self):
        # 重建前记下选中的是哪一行（按 self.rows 的下标，不是 iid——iid 是页内位置，
        # 换页后会重排）。重建后它还在本页就还选它，否则把编辑区的 _edit_key 作废：
        # 不然框里留着上一条的文本、key 还指着它，接着打字就会记错行。
        sel = self.tree.selection()
        old_ri = self.iid_to_idx.get(sel[0]) if sel else None
        self.tree.delete(*self.tree.get_children())
        self.iid_to_idx = {}
        pages = max(1, (len(self.filtered) + self.PAGE - 1) // self.PAGE)
        if self.page >= pages:
            self.page = pages - 1
        for pos, ri in enumerate(self._page_indices()):
            r = self.rows[ri]
            key = (r.get("集合", ""), r.get("文件", ""), r.get("文件内序号", ""))
            tag = self._tag_of(self.decisions.get(key, {}))
            iid = str(pos)
            self.iid_to_idx[iid] = ri
            self.tree.insert("", "end", iid=iid, tags=(tag,), values=(
                r.get("集合", ""), r.get("文件", ""), r.get("文件内序号", ""),
                r.get("原标题", ""), r.get("原作者", ""), r.get("网站标题", ""),
                r.get("网站作者", ""), r.get("判定", "")))
        self.page_label["text"] = "第 %d / %d 页  (本页 %d 条)" % (
            self.page + 1, pages, len(self._page_indices()))
        back = None
        if old_ri is not None:
            for iid, ri in self.iid_to_idx.items():
                if ri == old_ri:
                    back = iid
                    break
        if back is None:
            kids = self.tree.get_children()
            # 加载完/翻页后没有任何选中时，自动选中本页第一条：不然右侧比对区和编辑区
            # 全是空的，看着就像这两块界面根本没接线。
            back = kids[0] if kids else None
        # 重画前先把当前框里的值落定：筛选/翻页/批量按钮都不该悄悄吃掉刚打的字。提交用的
        # 是老的 _edit_key（下面才清），所以只会记到它自己那一行上；清掉 _edit_key 之后
        # 框里剩下的文本不会再被误记到别的行。
        self._commit_current()
        self._edit_key = self._edit_row = None
        self._cur_meta = {}
        if back is not None:
            self.tree.selection_set(back)
            self.tree.see(back)
            self.on_select(None)          # 判定被批量改过，编辑区要跟着回填

    def prev_page(self):
        if self.page > 0:
            self.page -= 1
            self.refresh_tree()

    def next_page(self):
        pages = max(1, (len(self.filtered) + self.PAGE - 1) // self.PAGE)
        if self.page < pages - 1:
            self.page += 1
            self.refresh_tree()

    # ---------------------------------------------------------- 详情编辑
    def on_select(self, _e):
        sel = self.tree.selection()
        if not sel:
            self._edit_key = self._edit_row = None
            self._cur_meta = {}
            return
        ri = self.iid_to_idx.get(sel[0])
        if ri is None:
            self._edit_key = self._edit_row = None
            self._cur_meta = {}
            return
        # 换行前先把上一条框里的值落定（否则「改了没回车就点下一条」= 白改）
        self._commit_current()
        r = self.rows[ri]
        key = (r.get("集合", ""), r.get("文件", ""), r.get("文件内序号", ""))
        self._edit_key, self._edit_row = key, r
        # 报告快照那一栏先立起来（_refresh_compare 里可能中途 return，来不及写），随后
        # _set_cmp_info 会拿现场比对结果把它刷新成「快照 + 实时比对」两行
        self._update_detail_info(None)
        # 先刷比对区：它会读出本条的正文元信息（写哪个键/什么形状/能不能写），下面填正文框
        # 要用；它自己也要靠 _edit_key 才知道网上正文该缓存到哪一行。
        self._refresh_compare(r)
        dec = self.decisions.get(key, {})
        # 「原文」= 比对基线，不是报告快照本身：这一轮里写回过的行，基线已经挪到刚写下去
        # 的值上，框里就该显示它（否则框里躺着旧值，一失焦又记成一处在改的修改）。
        base = self._base_vals(key, r)
        old_a = base["author"]
        site_a = (r.get("网站作者") or "").strip()
        old_t = base["title"]
        site_t = (r.get("网站标题") or "").strip()
        self.detail["author_old"] = old_a
        self.detail["author_site"] = site_a
        self.detail["title_old"] = old_t
        self.detail["title_site"] = site_t
        self.detail["link"] = (r.get("网站链接") or "").strip()

        # 框里装「本条将要写回的值」：判定里有值就用它，否则用原文（原文 = 本条不改）。
        # radio 是**派生显示**：_mark_* 按框里的文本反推该点亮哪一个，所以「采用网站值」
        # 的历史判定换到新行后仍会正确显示，而用户随手一改就会落回「自定义」。
        da = dec.get("author")
        dt = dec.get("title")
        self._set_entry_text(self.a_entry, old_a if da is None else da)
        self._mark_author()
        self._set_entry_text(self.t_entry, old_t if dt is None else dt)
        self._mark_title()
        self._fill_body_box(dec.get("body"), self._meta_by_key.get(key) or {})

        self.note_box.delete("1.0", "end")
        self.note_box.insert("1.0", r.get("说明", ""))
        self._update_save_buttons()

    # ---------------------------------------------------------- 比对区
    def _set_text(self, box, text):
        """往只读 Text 里写内容的唯一入口（disabled 状态下插不进去）。"""
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)
        box.configure(state="disabled")

    def _render_side(self, box, title, author, lines, marks=()):
        """把一边的标题/作者/正文排进只读框；marks 里的**正文行**标黄。

        文本必须与 format_side 逐字一致（并排两块要能逐行对），所以不自己拼字符串，
        而是照 format_side_parts 的版式逐行 insert——要标黄只能精确到行，没有别的办法。
        超长（古文观止单条 16 万字）就退回整块截断：差异行多半落在截断之外，标黄没意义。
        """
        head, body = format_side_parts(title, author, lines)
        text = format_side(title, author, lines)
        if len(text) > TXT_CAP:
            self._set_text(box, text[:TXT_CAP] + "\n…（已截断，完整 %d 字符）" % len(text))
            return
        box.configure(state="normal")
        box.delete("1.0", "end")
        if head:
            box.insert("end", "\n".join(head) + "\n")
            if body:
                box.insert("end", "\n")     # 头部与正文之间空一行，和 format_side 对齐
        for i, ln in enumerate(body):
            # 换行符单独 insert：tag 只圈这一行的字，不然黄底会拖到下一行行首
            box.insert("end", ln, ("diff",) if i in marks else ())
            box.insert("end", "\n")
        box.configure(state="disabled")

    def _load_local(self, r) -> dict:
        """读源 JSON 里这一条。取法与 crosscheck 建报告时完全一致（load_json +
        get_body + pick_title），所以面板里的正文就是当初算相似度用的那段文本。"""
        out = {"rec": None, "title": "", "author": "", "body": [], "err": "",
               "field": "", "kind": "", "ok": False, "reason": "", "orig": ""}
        coll_name = r.get("集合", "")
        base = r.get("文件", "")
        coll = {c.name: c for c in self.S.COLLECTIONS}.get(coll_name)
        if not coll:
            out["err"] = "未知集合 %s" % coll_name
            return out
        key = (coll_name, base)
        path = self._path_cache.get(key)
        if path is None:
            path = resolve_path(self.S, coll_name, base)
            self._path_cache[key] = path
        if not path:
            out["err"] = "找不到源文件 %s/%s" % (coll_name, base)
            return out
        try:
            idx = int(r.get("文件内序号", ""))
        except (TypeError, ValueError):
            out["err"] = "文件内序号无法解析"
            return out
        data = self._shard_cache.get(path)
        if data is None:
            try:
                data = self.S.load_json(path)
            except Exception as e:
                out["err"] = "读源文件失败：%s" % e
                return out
            if isinstance(data, dict):
                data = [data]
            self._shard_cache[path] = data
            while len(self._shard_cache) > 8:
                self._shard_cache.popitem(last=False)
        if not 0 <= idx < len(data):
            out["err"] = "序号 %d 越界（该文件 %d 条）" % (idx, len(data))
            return out
        rec = data[idx]
        if not isinstance(rec, dict):
            out["err"] = "该条不是对象"
            return out
        out["rec"] = rec
        out["body"] = self.S.get_body(rec, coll)
        out["title"] = self.S.pick_title(rec, coll, out["body"])
        out["author"] = (rec.get(coll.author) or "") if coll.author else ""
        if not isinstance(out["author"], str):
            out["author"] = ""

        # 正文字段名与形状。get_body 有「coll.body → paragraphs → content」三级兜底，所以
        # 写回时不能想当然用 coll.body —— 得看它**实际**是从哪个键读出来的，写错键等于往
        # 记录里插一个谁也不读的新字段。形状也要记住：本来是一个字符串的不能写回成数组，
        # 反之亦然（两种写法 get_body 都读得出来，但文件结构会变）。
        bf = coll.body
        v = rec.get(bf)
        if v is None and bf != "paragraphs":
            bf, v = "paragraphs", rec.get("paragraphs")
        if v is None:
            bf, v = "content", rec.get("content")
        if v is None:
            # 三级都没有：按集合声明的字段名新建一个数组（insert 是允许的，键名也必须是
            # 集合自己声明的那个，见 _body_fields）
            out["field"], out["kind"], out["ok"] = coll.body, "list", True
        elif isinstance(v, str):
            out["field"], out["kind"], out["ok"], out["orig"] = bf, "str", True, v
        elif isinstance(v, list) and all(isinstance(x, str) for x in v):
            out["field"], out["kind"], out["ok"] = bf, "list", True
            out["orig"] = "\n".join(v)
        else:
            # 嵌套结构（蒙学「content: [{title, content:[…]}]」这类）：面板里的正文是
            # _flatten 拍出来的，照拍平结果写回去就把结构毁了 —— 宁可不给改。
            out["field"], out["reason"] = bf, (
                "正文键 %s 是嵌套结构（%s），面板里的正文是拍平读出来的；照扁平结果写回去"
                "会毁掉结构，这里不让改，请直接改源文件" % (bf, type(v).__name__))
            # 不能改归不能改，框里还是把「现在读到的正文」摆出来（只读），否则这一栏是空的，
            # 用户没法跟右边网上那栏对着看
            out["orig"] = "\n".join(str(x) for x in out["body"])
        if out["ok"] and len(out["orig"]) > BODY_EDIT_CAP:
            out["ok"] = False
            out["reason"] = ("正文 %d 字，超过编辑框上限 %d 字；这么大的条目请点「完整 JSON」"
                             "看，或先拆分源文件" % (len(out["orig"]), BODY_EDIT_CAP))
        return out

    def _refresh_compare(self, r):
        """切换条目时刷新比对区。本地侧同步读（有缓存，够快），网上侧交给后台线程。"""
        self._cmp_row = r                 # 摘要行在详情页到之前先拿报告列比标题/作者
        try:
            loc = self._load_local(r)
        except Exception as e:
            # 读源文件出错也得分明地刷成本行的状态：面板留着上一条的内容，
            # 用户会以为「选了新数据但没更新」，还照着旧内容做判断。
            _log_exc("[读本地记录失败] %r\n%s" % (r, traceback.format_exc()))
            loc = {"rec": None, "title": "", "author": "", "body": [],
                   "err": "读源文件出错：%s" % e}
        self._cmp_local = loc
        # 网上正文的行缓存：本条还没取到时置空，「采用网站值」和写回后的「正文是否已一致」
        # 都靠它。它在 _show_site 里按当前选中行填回来。
        self._cmp_site_lines = []
        self._cmp_marks = (set(), set())   # 正文标黄行号；同上，待 _show_site 填
        key = (r.get("集合", ""), r.get("文件", ""), r.get("文件内序号", ""))
        self._meta_by_key[key] = {
            "field": loc.get("field", ""), "kind": loc.get("kind", ""),
            "ok": bool(loc.get("ok")), "orig": loc.get("orig", ""),
            # 读不到记录时没有 reason，用 err 顶（否则提示会变成「原因未知」）
            "reason": loc.get("reason") or loc.get("err") or "读不到本地记录"}

        # 本地侧走解析后的「标题/作者/正文」，与网上侧同版式；超长记录（古文观止
        # 单条 16 万字）和原始 JSON 都由 _render_side / 「完整 JSON」兜着
        if loc["rec"] is None:
            self._set_text(self.local_box, "（读不到本地记录：%s）" % (loc["err"] or "未知原因"))
        else:
            self._render_side(self.local_box, loc["title"], loc["author"], loc["body"])
        self.local_full_btn.configure(state=("normal" if loc["rec"] is not None else "disabled"))

        # 报告列是历史快照，源文件可能已被上一轮写回改过 —— 不一致要说清楚，
        # 否则用户会以为程序读错了记录。
        warn = ""
        if loc["rec"] is not None:
            ra, rt = (r.get("原作者") or "").strip(), (r.get("原标题") or "").strip()
            if clean(ra) != clean(loc["author"]) or clean(rt) != clean(loc["title"]):
                warn = "· 源文件已改过（现「%s／%s」，报告写「%s／%s」）" % (
                    loc["author"] or "?" , loc["title"] or "?", ra or "?", rt or "?")

        sid = link_sid(r)
        if not sid:
            self._cmp_warn = warn
            self.site_label["text"] = "网上内容"
            self._set_text(self.site_box, "本行没有网站链接，无法比对。")
            self._set_cmp_info(loc, None, "", warn)
            return

        # 优先本地缓存：命中就当场显示，不惊动后台
        raw = read_cached_detail(self._site_html_dir, sid)
        if raw:
            res = parse_detail_html(raw)
            if res:
                self._show_site(sid, res, "本地缓存", warn)
                return
        if self._online:
            self._cmp_warn = warn
            self.site_label["text"] = "网上内容（详情页抓取中…）"
            self._set_text(self.site_box, (
                "正在抓取 shi-%d 的详情页…\n"
                "（站点忙时最多要等十几秒；这段摘要行先按报告列里的网站标题/作者比对）" % sid))
            self._set_cmp_info(loc, None, "抓取中…", warn)
            self._request_site(sid)
        else:
            self._show_site(sid, ref_lines(self._site_db, sid), "列表页预览（未联网）", warn)

    def _set_cmp_info(self, loc, res, src, warn=""):
        """摘要行：**哪一项不一致就列哪一项**，全一致/还没比出来时才把各列一致项列全。

        why 标题/作者要**无条件参与比对**（不是无条件显示）：详情页要联网、要排队、还可能
        抓不到（站点 503 时 4 次退避最长要 ~15 秒，抓失败后如果 ref.db 也没这条就什么都
        没有）。要是把比对挂在 `if res:` 上，详情页没到时摘要里根本没有标题/作者两个字，
        看着就像没比过——这正是「作者与标题没有进行对比」的由来。报告行本来就带着 网站标题/
        网站作者，先拿它当场比一次，详情页到了再升级成详情页的值；不一致时才显示不一致的
        那项（另加一句「只列不一致项」说明），全一致时照旧全列。
        """
        loc = loc or {}
        # 网上侧取值优先级：详情页/ref.db > 报告列（列表页快照）。取到哪一级就标哪一级，
        # 别让人以为「标题一致」是拿详情页比的——它可能只是列表页。
        row = getattr(self, "_cmp_row", None) or {}
        rtitle = (res or {}).get("title") or ""
        rauth = (res or {}).get("author") or ""
        side = "报告列（列表页快照）"
        if rtitle or rauth:
            side = "详情页" if src in ("本地缓存", "实时抓取") else "列表页"
        # 逐字段补缺：ref.db 里有些行作者是空的（详情页 h1 也是），缺哪个就从报告列补哪个。
        # why 不能「有 title 就整行走详情页」：那样作者一侧空着，`if la and ra` 直接不成立，
        # 「作者一致/不一致」就整条不出现——又是一次「作者没有进行对比」。
        gap = False
        if not rtitle:
            rtitle, gap = row.get("网站标题") or "", True
        if not rauth:
            rauth, gap = row.get("网站作者") or "", True
        if gap and res and (rtitle or rauth):
            side += "／报告列补缺"
        # 标题/作者都先用 clean/canon 归一再比：PUNCT 本来就吃掉《》，所以详情页 h1 里的
        # 「《饮马长城窟行》」和本地 JSON 的「饮马长城窟行」算同一个标题。
        lt, rt = bare_title(loc.get("title") or ""), bare_title(rtitle)
        la, ra = canon_author(loc.get("author") or ""), canon_author(rauth)
        # 不一致的项与一致的项分开攒，**先把不一致的列出来**：三项一律列的话，真正该动手
        # 的那一项会被「标题一致    作者一致」淹掉，一眼扫过去还得自己找。反过来，三项都
        # 不冲突、或现场结果还没到（详情页在抓）时，就把能比的项照旧全列出来——否则摘要
        # 只剩「本地 10 句/120 字」，看着像根本没比对过，那正是「作者与标题没有进行对比」
        # 的由头。
        diff, same = [], []
        if lt and rt:
            hit = clean(lt) == clean(rt)
            (same if hit else diff).append("标题%s" % ("一致" if hit else "不一致"))
        if la and ra:
            hit = la == ra
            (same if hit else diff).append("作者%s" % ("一致" if hit else "不一致"))
        body_same = None
        net = ""
        if res:
            ls = res.get("lines") or []
            net = "网上 %d 句/%d 字" % (len(ls), sum(len(x) for x in ls))
            # 列表页预览是截断文本，拿它算相似度只会误导（长诗必然偏低）
            if loc.get("body") and ls and src in ("本地缓存", "实时抓取"):
                body_same = clean("".join(loc["body"])) == clean("".join(ls))
                if body_same:
                    same.append("正文一致（对全文）")
                else:
                    diff.append("正文不一致（对全文）")
                    diff.append("相似度 %s" % similarity(
                        clean("".join(loc["body"])), clean("".join(ls))))
        parts = list(diff or same)
        if (lt or la) and (rt or ra):
            parts.append("网上侧：%s" % side)
        if loc.get("body"):
            parts.append("本地 %d 句/%d 字" % (
                len(loc["body"]), sum(len(x) for x in loc["body"])))
        if net:
            parts.append(net)
        if diff and same:
            # 藏了「已一致」的项就得说一声，不然「作者没出现在摘要里」会被读成没比过
            parts.append("· 只列不一致项（未列出的已一致）")
        if src:
            parts.append("来源：%s" % src)
        marks = getattr(self, "_cmp_marks", None) or ((), ())
        if len(marks[0]) or len(marks[1]):
            parts.append("黄底＝与另一侧有差异的行（左 %d / 右 %d）" % (
                len(marks[0]), len(marks[1])))
        if warn:
            parts.append(warn)
        self.cmp_info["text"] = "    ".join(parts)
        self._update_detail_info({"body_same": body_same, "src": src})

    def _update_detail_info(self, live=None):
        """编辑区顶上那一栏。**必须分开写「报告快照」和「实时比对」两行。**

        why：报告列（判定/标题相似度/正文相似度）是建报告那一刻算的，那份报告里的正文
        相似度出自站点**列表页预览**（长诗必然被截断）；而下面比对带是拿**详情页全文**
        现场算的。同一行两处口径不同，于是出现「上面写正文不一致、下面显示一致」——
        用户看到的就是程序自相矛盾。两行分开、并把分歧的原因点出来，才不至于让人
        以为读错了数据。

        live: None＝还没有现场结果；否则 {"body_same": True/False/None, "src": str}
        """
        r = getattr(self, "_edit_row", None) or {}
        info = ["判定（报告快照）：%s" % (r.get("判定", "") or "—"),
                "标题相似度 %s   正文相似度 %s（快照，取自列表页摘要）" % (
                    r.get("标题相似度", "") or "—", r.get("正文相似度", "") or "—"),
                "文件：%s#%s" % (r.get("文件", ""), r.get("文件内序号", ""))]
        if live is None:
            info.append("实时比对：等选中条目的网上正文到位")
        elif not link_sid(r):
            # 没有链接的行连「抓取中」都算不上：不改口的话这里会显示「详情页还在抓」，
            # 让人一直等一个永远不会来的结果。
            info.append("实时比对：本行没有网站链接，判不了")
        else:
            src = live.get("src") or ""
            same = live.get("body_same")
            if same is True:
                tail = "（对详情页全文）"
                if "正文" in (r.get("判定") or ""):
                    tail += "——上面那份快照与它不一致：快照是按列表页的截断摘要算的"
                info.append("实时比对：正文一致" + tail)
            elif same is False:
                info.append("实时比对：正文不一致（对详情页全文）")
            elif src.startswith("列表页预览"):
                info.append("实时比对：网上侧只有列表页摘要（长诗被截断），判不了正文全不全")
            elif not src or src == "抓取中…":
                info.append("实时比对：详情页还在抓，先看上面那份快照")
            else:
                info.append("实时比对：没取到网上正文（%s），判不了" % src)
        self.detail["info"]["text"] = "\n".join(info)

    def _show_site(self, sid, res, src, warn=""):
        res = res or {}
        lines = res.get("lines") or []
        title = (res.get("title") or "").strip()
        # 网上正文按**当前选中行**存一份：编辑区「采用网站值」要用它，写回后判断「正文
        # 是否已和网站一致」也要用它。_show_site 一定是为当前行渲染的（旧行的结果在
        # _poll_site 里被令牌过期挡掉了），所以这里的 _edit_key 就是这一行。
        self._cmp_site_lines = list(lines)
        if lines and self._edit_key is not None:
            self._site_lines_by_key[self._edit_key] = list(lines)
        author = (res.get("author") or "").strip()
        if not lines and not title:
            # src 要带上失败原因（_site_worker 会填）；这里空着就说明三档全落空，
            # 光写「没取到内容」用户没法判断是该重试还是该关联网。
            self.site_label["text"] = "网上内容（没取到）"
            self._set_text(self.site_box, "没取到网上内容——%s" % (src or "原因未知"))
            self._cmp_marks = (frozenset(), frozenset())    # 没正文可比，清掉上一轮的黄底
            self._set_cmp_info(self._cmp_local, None, src, warn)
            return
        tag = src
        if src.startswith("列表页预览"):
            tag += "，长诗可能被截断"
        self.site_label["text"] = "网上内容（%s）" % tag

        # 正文差异逐行标黄。这里是**唯一**两边都到手的地方（左边刚由 _refresh_compare
        # 读进来，右边就是这一份），所以标黄也只在这里算。只有拿到全文（本地缓存/实时
        # 抓取）才标：列表页预览是截断的，拿它比会把后半段整片标黄，比不标还误导。
        loc = self._cmp_local or {}
        ma = mb = frozenset()
        if src in ("本地缓存", "实时抓取") and loc.get("rec") is not None:
            ma, mb = line_marks(loc.get("body") or [], lines)
        self._cmp_marks = (ma, mb)
        if loc.get("rec") is not None:
            # 本地侧重画一遍才能打上标记（_refresh_compare 里画的那次还没有网上正文）
            self._render_side(self.local_box, loc.get("title"), loc.get("author"),
                              loc.get("body") or [], ma)
        self._render_side(self.site_box, title, author, lines, mb)
        self._set_cmp_info(loc, res, src, warn)

    # ---------------------------------------------------------- 后台抓取
    def _toggle_online(self):
        self._online = bool(self.online_var.get())
        if not self._online:
            self.set_status("已关闭联网：只读本地缓存，抓不到的用 ref.db 列表页预览兜底")

    def _request_site(self, sid):
        # 同一条已经在抓（或已排队）就不再排第二次。why 需要这个去重：refresh_tree / _goto_next
        # 里 selection_set 的虚拟事件和显式 on_select 会让同一行走两次（实测一行触发 2 次）。
        # 第二次请求会被第一次刚写好的缓存满足，于是明明是刚抓的，界面却标成「本地缓存」
        # ——来源归因就错了。
        with self._want_lock:
            if self._inflight_sid == sid or (self._want and self._want[0] == sid):
                return
            self._token += 1
            self._inflight = True
            self._inflight_sid = sid
            self._want = (sid, self._token)
        self._wake.set()
        self.root.after(120, self._poll_site)

    def _poll_site(self):
        """主线程轮询抓取结果。令牌过期的直接丢——用户已经切到别的条目了。"""
        while True:
            try:
                sid, tok, res, src = self._siteq.get_nowait()
            except queue.Empty:
                break
            if tok == self._token:
                self._inflight = False
                self._inflight_sid = None
                self._show_site(sid, res, src, getattr(self, "_cmp_warn", ""))
        if self._inflight:
            self.root.after(120, self._poll_site)

    def _site_worker(self):
        """子线程：只做网络/磁盘/sqlite。why 单线程 + 新请求顶掉旧请求：快速点十几条
        也不会并发打站。这里绝不能出现任何 tkinter 调用。"""
        while True:
            self._wake.wait()
            self._wake.clear()
            with self._want_lock:
                job, self._want = self._want, None
            if not job:
                continue
            sid, tok = job
            res, src = {}, ""
            try:
                raw = read_cached_detail(self._site_html_dir, sid)
                if raw:
                    res, src = parse_detail_html(raw), "本地缓存"
                if not res.get("lines") and self._online:
                    raw = fetch_detail_html(sid, self._site_html_dir)
                    if raw:
                        res, src = parse_detail_html(raw), "实时抓取"
                if not res.get("lines"):
                    r = ref_lines(self._site_db, sid)
                    if r:
                        res, src = r, "列表页预览"
            except Exception as e:
                res, src = {}, "出错：%s" % e
            if not res:
                # 三档（详情页缓存 / 联网抓 / ref.db）全落空时 src 会是空的，界面上
                # 就只能显示「原因未知」。说清楚是哪一档断的，用户才知道要不要重试。
                src = src or ("没联网，且本地无缓存、ref.db 也没这条" if not self._online
                              else "详情页抓不到（本地无缓存、ref.db 也没这条）")
            self._siteq.put((sid, tok, res, src))

    def _open_full_json(self):
        rec = (self._cmp_local or {}).get("rec")
        if rec is None:
            return
        tk, scrolledtext = self.tk, self.scrolledtext
        w = tk.Toplevel(self.root)
        w.title("完整 JSON")
        w.geometry("900x700")
        box = scrolledtext.ScrolledText(w, wrap="none")
        box.pack(fill="both", expand=True)
        box.insert("1.0", json.dumps(rec, ensure_ascii=False, indent=2))
        box.configure(state="disabled")

    def _set_entry_text(self, entry, txt):
        entry.delete(0, "end")
        if txt:
            entry.insert(0, txt)

    def _sync_author_entry(self):
        """点 radio：把该档的值填进框。「自定义」只是把焦点给框——文本要让用户自己敲。"""
        mode = self.a_var.get()
        if mode == "keep":
            self._set_entry_text(self.a_entry, self.detail["author_old"])
        elif mode == "site":
            self._set_entry_text(self.a_entry, self.detail["author_site"])
        else:
            self.a_entry.focus_set()

    def _sync_title_entry(self):
        mode = self.t_var.get()
        if mode == "keep":
            self._set_entry_text(self.t_entry, self.detail["title_old"])
        elif mode == "site":
            self._set_entry_text(self.t_entry, self.detail["title_site"])
        else:
            self.t_entry.focus_set()

    def _mark_author(self):
        """打字后反推 radio：等于原文→保持原值，等于网站值→采用网站值，其余→自定义。"""
        t = self.a_entry.get().strip()
        if clean(t) == clean(self.detail["author_old"]):
            self.a_var.set("keep")
        elif self.detail["author_site"] and clean(t) == clean(self.detail["author_site"]):
            self.a_var.set("site")
        else:
            self.a_var.set("custom")

    def _mark_title(self):
        t = self.t_entry.get().strip()
        if clean(t) == clean(self.detail["title_old"]):
            self.t_var.set("keep")
        elif self.detail["title_site"] and clean(t) == clean(self.detail["title_site"]):
            self.t_var.set("site")
        else:
            self.t_var.set("custom")

    def _sync_body_box(self):
        """点正文那一排 radio：把该档的值填进正文框（「自定义」只给焦点）。

        「采用网站值」搬的是右边网上正文那一栏——本条网上正文还没取到（没联网、没缓存、
        站点没这条）时说清楚，而不是默默什么都不做。
        """
        mode = self.b_var.get()
        if not (self._cur_meta or {}).get("ok"):
            return
        if mode == "keep":
            txt = self._cur_meta.get("orig") or ""
        elif mode == "site":
            if not self._cmp_site_lines:
                self.set_status("本条还没取到网上正文（可勾上「联网」或先点一下这一行）")
                self._mark_body()
                return
            txt = "\n".join(self._cmp_site_lines)
        else:
            self.b_box.focus_set()
            return
        self.b_box.configure(state="normal")
        self.b_box.delete("1.0", "end")
        self.b_box.insert("1.0", txt)
        self._commit_current()        # 填完当场记账，效果和手工打字一样
        self._mark_body()

    @staticmethod
    def _field_value(text, old):
        """框里的文本 → 判定值。空 或 与原文归一同 → None（= 本字段不改）。"""
        t = (text or "").strip()
        if not t or clean(t) == clean((old or "").strip()):
            return None
        return t

    @staticmethod
    def _body_value(text, meta):
        """正文框里的文本 → 判定值（写回源文件时用的那个值）。

        空、与原文归一同、或这条本来就不可写 → None（= 正文不改）。形状跟着源文件：
        本来是字符串的写回字符串，本来是数组的按行拆成数组——两种写法 get_body 都读得
        出来，但结构变了就是另一种「写坏」，所以必须保持原样。
        """
        t = (text or "").strip()
        if not t or not meta or not meta.get("ok"):
            return None
        if clean(t) == clean(meta.get("orig") or ""):
            return None
        if meta.get("kind") == "str":
            return t
        return [ln.strip() for ln in t.splitlines() if ln.strip()]

    def _fill_body_box(self, val, meta):
        """把本条的正文判定值（或原文）装进正文框；不能写的条目锁上框并写明原因。"""
        meta = meta or {}
        self._cur_meta = meta
        box = self.b_box
        box.configure(state="normal")
        box.delete("1.0", "end")
        if not meta.get("ok"):
            # 只读展示，同样按 TXT_CAP 截一下：超长条目（古文观止单条 16 万字）整段塞进
            # 只读框会把界面拖垮，而这里本来就只是为了「对着看」，写回已被拒绝
            show = meta.get("orig") or ""
            if len(show) > TXT_CAP:
                show = show[:TXT_CAP] + "\n…（已截断，完整 %d 字符）" % len(show)
            box.insert("1.0", show)
            box.configure(state="disabled")
            self.b_hint["text"] = "本条正文不能回写：%s" % (
                meta.get("reason") or "读不到本地记录")
        else:
            txt = meta.get("orig") if val is None else val
            if isinstance(txt, (list, tuple)):
                txt = "\n".join(str(x) for x in txt)
            box.insert("1.0", txt or "")
            self.b_hint["text"] = "写回字段：%s（%s）" % (
                meta.get("field") or "?",
                "源文件里是单个字符串" if meta.get("kind") == "str" else "源文件里是数组，按行写回")
        self._mark_body()

    def _mark_body(self):
        """打字后反推 radio：等于原文→保持原值，等于网上正文→采用网站值，其余→自定义。"""
        meta = self._cur_meta or {}
        if not meta.get("ok"):
            self.b_var.set("keep")
            return
        t = self.b_box.get("1.0", "end").strip()
        if clean(t) == clean(meta.get("orig") or ""):
            self.b_var.set("keep")
        elif self._cmp_site_lines and clean(t) == clean("\n".join(self._cmp_site_lines)):
            self.b_var.set("site")
        else:
            self.b_var.set("custom")

    def _base_vals(self, key, r):
        """这一条作者/标题的比对基线（没有就按报告快照立一份）。见 __init__ 的 _base_by_key。"""
        r = r or {}
        return self._base_by_key.setdefault(key, {
            "author": (r.get("原作者") or "").strip(),
            "title": (r.get("原标题") or "").strip()})

    def _commit_current(self):
        """把框里的值记进 self._edit_key 那一条的判定（打字/失焦/回车/换行/保存时都调）。

        「跳过本条」不再是按钮：把框清空或填回原文，本字段就记 None；_collect_plan 会
        直接跳掉 None，等于这条不写。
        """
        key, r = self._edit_key, self._edit_row
        if key is None or r is None:
            return
        base = self._base_vals(key, r)
        dec = {"author": self._field_value(self.a_entry.get(), base["author"]),
               "title": self._field_value(self.t_entry.get(), base["title"]),
               "body": self._body_value(self.b_box.get("1.0", "end"),
                                        self._meta_by_key.get(key)),
               "has_body": "正文" in r.get("判定", "")}
        if self.decisions.get(key) != dec:
            self.decisions[key] = dec
            self._retag(key)

    def _on_edit_key(self, e=None):
        self._commit_current()
        w = getattr(e, "widget", None)
        if w is self.a_entry:
            self._mark_author()
        elif w is self.t_entry:
            self._mark_title()
        elif w is self.b_box:
            self._mark_body()

    def _on_edit_enter(self, _e=None):
        self._commit_current()
        self._goto_next()
        return "break"

    def _goto_next(self):
        """选中下一行。落定本条由 on_select 开头的 _commit_current 负责。"""
        sel = self.tree.selection()
        if not sel:
            return
        sibs = self.tree.get_children()
        if sel[0] not in sibs:
            return
        idx = sibs.index(sel[0])
        if idx + 1 < len(sibs):
            nxt = sibs[idx + 1]
            self.tree.selection_set(nxt)
            self.tree.see(nxt)
            self.on_select(None)

    def page_apply_site(self):
        for ri in self._page_indices():
            r = self.rows[ri]
            key = (r.get("集合", ""), r.get("文件", ""), r.get("文件内序号", ""))
            d = default_decision(r)
            # 强制采用网站值（即便默认判为需人工也照用，用户自己担责）
            new_a = (r.get("网站作者") or "").strip()
            new_t = (r.get("网站标题") or "").strip()
            d["author"] = new_a if (new_a and new_a not in ANON) else d["author"]
            sim = 0
            try:
                sim = float(r.get("标题相似度") or 0)
            except ValueError:
                pass
            d["title"] = new_t if (new_t and clean((r.get("原标题") or "")) != clean(new_t)) else d["title"]
            self.decisions[key] = d
        self._after_bulk("本页已设为采用网站值")

    def page_skip(self):
        for ri in self._page_indices():
            r = self.rows[ri]
            key = (r.get("集合", ""), r.get("文件", ""), r.get("文件内序号", ""))
            self.decisions[key] = {"author": None, "title": None, "body": None,
                                   "has_body": "正文" in r.get("判定", "")}
        self._after_bulk("本页已设为跳过")

    def _after_bulk(self, msg):
        """批量按钮收尾：作废当前行，再重画一遍。

        refresh_tree 开头那次「落定」是给「打字后翻页/筛选」用的（别把刚打的字吃掉），但它
        会把编辑框里的旧文本盖回 _edit_key 那一条——批量按钮刚改的是**整页**判定，当前这条
        要是被盖回去，用户点了「本页跳过」自己坐着的那条却还留着原来的修改。作废之后那次落定
        就是空操作，refresh_tree 末了用新判定把编辑框回填成「不改」的原文。
        """
        self._edit_key = self._edit_row = None
        self.refresh_tree()
        self.set_status(msg)

    # ---------------------------------------------------------- 写回
    def _plan_row(self, key, dec):
        """**一条**判定 → 写盘计划 {(集合,文件): [(下标, 字段, 新值)]}；没什么可写就 {}。

        正文那一项要换成**源文件里真实的字段名**（paragraphs/content/para/lines…，选中
        时由 _load_local 探明），不能拿 "body" 当字段名写进去——那会插一个谁也不读的键。
        """
        dec = dec or {}
        try:
            coll, base, idx = key[0], key[1], int(key[2])
        except (TypeError, ValueError, IndexError):
            return {}
        edits = []
        for fld in ("author", "title"):
            val = dec.get(fld)
            if val is not None:
                edits.append((idx, fld, val))
        bval = dec.get("body")
        meta = self._meta_by_key.get(key) or {}
        if bval is not None and meta.get("ok") and meta.get("field"):
            edits.append((idx, meta["field"], bval))
        return {(coll, base): edits} if edits else {}

    def _collect_plan(self):
        """工作表里**所有**判定 → 写盘计划（「全部写回」和「导出瘦身报告」用）。"""
        plan = defaultdict(list)
        for key, dec in self.decisions.items():
            for (coll, base), edits in self._plan_row(key, dec).items():
                plan[(coll, base)].extend(edits)
        return plan

    def _pending_rows(self):
        """工作表里还挂着待写回修改的行：[(键, 处数)]。"""
        out = []
        for key, dec in self.decisions.items():
            n = sum(len(v) for v in self._plan_row(key, dec).values())
            if n:
                out.append((key, n))
        return out

    def _update_save_buttons(self):
        """「全部写回」按钮上的处数跟着判定实时变。

        why 非得实时：这个数字是用户判断「还有没有没落的改动」的唯一依据，停在打开报告
        那一刻的数字就等于报错数。
        """
        if getattr(self, "save_all_btn", None) is None:
            return
        n = sum(c for _k, c in self._pending_rows())
        self.save_all_btn.configure(text=("全部写回（%d 处）" % n) if n else "全部写回")

    def do_save(self):
        """**只保存当前这一条**。

        why 从原来的「写全表」改过来：这个按钮就长在当前这一条的三个框底下，按下去却把
        工作表里所有积攒的修改一起落盘——没法只落一条，也不知道顺手写掉了哪些。其余行
        的改动照旧留在工作表里，点「全部写回（N 处）」一次性落。
        """
        self._commit_current()      # 刚敲完还没回车的那一条也要算进去
        key = self._edit_key
        if key is None:
            self.messagebox.showinfo("没选中条目", "先在表格里选中一行，再保存它的修改。")
            return
        plan = self._plan_row(key, self.decisions.get(key) or {})
        total = sum(len(v) for v in plan.values())
        if total == 0:
            # 「保存本行修改」的作用是把**这一条从报告里划掉**，改不改 JSON 只是顺带的事：
            # 用户看过这一条、判定不必改（或已照网站手工改在别处），按下去它就该跟写过盘的
            # 行一样消失——否则下次加载又原样冒出来，只能靠眼睛记「这条我看过了」。
            self._prune_current_row()
            return
        r = self._edit_row or {}
        rest = sum(c for _k, c in self._pending_rows()) - total
        if not self.dry_var.get():
            ok = self.messagebox.askyesno("确认写盘",
                "将把**本条**（%s#%s）的 %d 处修改写回源 JSON（备份在 xcheck/patch-backup/）。\n"
                "写完后这一条会从报告里删掉（原样抄进「已处理-<时间戳>.csv」留档）。\n"
                "%s确定继续？"
                % (r.get("文件", ""), r.get("文件内序号", ""), total,
                   ("工作表里另有 %d 处修改，本次不动（要一起写就点「全部写回」）。\n" % rest)
                   if rest > 0 else ""))
            if not ok:
                return
        self._write_and_report(plan, dry=self.dry_var.get())

    def do_save_all(self):
        """把工作表里**所有**待写回的修改一次落盘（原来「保存修改」的行为）。"""
        self._commit_current()
        plan = self._collect_plan()
        total = sum(len(v) for v in plan.values())
        if total == 0:
            self.messagebox.showinfo("无需保存", "没有要应用的修改。")
            return
        if not self.dry_var.get():
            ok = self.messagebox.askyesno("确认写盘",
                "将把全部 %d 处修改（%d 条）写回源 JSON 文件（备份在 xcheck/patch-backup/）。\n"
                "写完后这些行会从报告里删掉（原样抄进「已处理-<时间戳>.csv」留档）。\n"
                "确定继续？" % (total, len(self._pending_rows())))
            if not ok:
                return
        self._write_and_report(plan, dry=self.dry_var.get())

    def export_only(self):
        plan = self._collect_plan()
        self._write_and_report(plan, dry=True, export_only=True)

    # ---------------------------------------------------------- 办完的行从报告里摘掉
    @staticmethod
    def _row_key(r):
        return (r.get("集合", ""), r.get("文件", ""), r.get("文件内序号", ""))

    @staticmethod
    def _applied_row(r, applied):
        """这一行是不是「本轮落了盘」的那一批（applied 里存的是 int 下标）。"""
        try:
            return (r.get("集合", ""), r.get("文件", ""), int(r.get("文件内序号", ""))) in applied
        except (TypeError, ValueError):
            return False

    def _unique_stamp(self, stamp):
        """给这一批的名字避重：名单与报告备份都按它走，同一秒里连落两次也不互相盖掉。

        why 不能直接用秒级时间戳：名单（已处理-<时间戳>.csv）与报告备份都是「一次保存一
        份」，秒级撞上就把前一批的字数抹了——备份尤其要命，那是报告改写**前**的原文，被
        覆盖后想找回上一批删了什么就只能翻 CSV 名单了。

        判据看的是「文件在不在」而不是「目录在不在」：patchjson 跟我们在同一个 BACKUP 父
        目录下按同一个秒级戳建目录，目录先被它建出来是常事，那不是撞名，报告备份正好跟那
        一批分片备份躺在一起。
        """
        rep = os.path.basename(self.csv_path) if self.csv_path else ""
        for n in range(1, 1000):
            sfx = "" if n == 1 else "-%d" % n
            arch = os.path.join(self.WORK, "已处理-%s%s.csv" % (stamp, sfx))
            copy = os.path.join(self.BACKUP, stamp + sfx, rep) if rep else ""
            if not os.path.exists(arch) and not (copy and os.path.exists(copy)):
                return stamp + sfx
        return stamp

    def _prune_applied(self, applied, stamp):
        """把本轮**落了盘**的行从工作表里摘掉，并在报告文件里就地删除（另存一份名单留档）。

        why：报告是一份待办清单。写完的行留在里面，下次加载又原样冒出来——用户翻到最后也
        分不清哪些是真没办、哪些早办完了；报告里那句「正文相似度」是建报告时拿列表页截断
        摘要算的，手工改过的正文行哪怕已经和网站一字不差，也未必能靠自动复核剔掉。

        删掉的行不是就地销毁：原样抄进 WORK/已处理-<时间戳>.csv（照报告的列序），报告改写
        前的**原文**再复制一份到 xcheck/patch-backup/<时间戳>/。报告自己按 .part + os.replace
        重写（同 crosscheck 落盘手法），中途崩了也不会留下半份报告让 GUI 当最新报告加载。

        返回 (摘掉的行数, 给「完成」对话框补的一段说明)；没有行可摘就是 (0, "")。
        """
        if not applied or not self.rows:
            return 0, ""
        removed, kept = [], []
        kept_old = []
        for i, r in enumerate(self.rows):
            if self._applied_row(r, applied):
                removed.append(r)
            else:
                kept.append(r)
                kept_old.append(i)
        if not removed:
            return 0, ""
        stamp = self._unique_stamp(stamp)
        # 列序照抄报告；报告列读不出来（手工打开的文件没有表头）就退回这一行的键序
        fields = self._report_fields or list(removed[0].keys())

        def dump(path, rows):
            with open(path + ".part", "w", encoding="utf-8-sig", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(fields)
                for r in rows:
                    w.writerow([r.get(h, "") for h in fields])
            os.replace(path + ".part", path)

        # 记住摘之前选中的是哪一条（按 key，不按下标——摘完下标就挪位了），稍后尽量选回去
        sel_key = None
        sel = self.tree.selection()
        if sel:
            ri = self.iid_to_idx.get(sel[0])
            if ri is not None and 0 <= ri < len(self.rows):
                sel_key = self._row_key(self.rows[ri])

        # ① 名单留档
        arch = ""
        try:
            arch = os.path.join(self.WORK, "已处理-%s.csv" % stamp)
            dump(arch, removed)
        except Exception as e:
            arch = ""
            _log_exc("已处理名单落盘失败：%s" % e)
        # ② 报告就地删行（先复制原文，再改）
        rep_note = ""
        if self.csv_path and os.path.exists(self.csv_path):
            try:
                bak_dir = os.path.join(self.BACKUP, stamp)
                os.makedirs(bak_dir, exist_ok=True)
                shutil.copy2(self.csv_path, os.path.join(bak_dir, os.path.basename(self.csv_path)))
                dump(self.csv_path, kept)
                rep_note = "报告已就地更新（原文备份 %s）" % os.path.join(
                    os.path.basename(self.BACKUP), stamp, os.path.basename(self.csv_path))
            except Exception as e:
                # 报告没改成不影响已经写回源 JSON 的那半件事，但必须说清楚：下次加载还会看到
                rep_note = "报告文件未能就地更新（%s），下次加载还会看到这几行" % e
                _log_exc("报告就地更新失败：%s" % e)

        # ③ 内存里同步收缩：filtered 存的是 self.rows 的下标，行挪了它必须跟着挪，否则
        # 分页会指到别的行上（写回时更会写错记录）
        new_pos = {old: new for new, old in enumerate(kept_old)}
        self.filtered = [new_pos[i] for i in self.filtered if i in new_pos]
        self.rows = kept
        gone = {self._row_key(r) for r in removed}
        for k in gone:
            self.decisions.pop(k, None)
            self._base_by_key.pop(k, None)
            self._meta_by_key.pop(k, None)
            self._site_lines_by_key.pop(k, None)
        if self._edit_key in gone:
            # 编辑区里正装着这一条，行没了它也就没主了：作废，别让框里的文本再被记到别人身上
            self._edit_key = self._edit_row = None
            self._cur_meta = {}
        # 树里那张表还按旧下标指着行，refresh_tree 又会照着 tree 的选中去找回旧下标——先撤掉
        # 选中再重建，免得它「找回」一个已经挪位的行
        try:
            self.tree.selection_remove(*self.tree.selection())
        except Exception:
            pass
        self.iid_to_idx = {}
        self.refresh_tree()
        if sel_key is not None and sel_key not in gone:
            for iid, ri in self.iid_to_idx.items():
                if self._row_key(self.rows[ri]) == sel_key:
                    self.tree.selection_set(iid)
                    self.tree.see(iid)
                    self.on_select(None)
                    break

        out = "已从报告里摘掉 %d 行" % len(removed)
        if arch:
            out += "（名单 %s）" % os.path.basename(arch)
        return len(removed), (out + "；" + rep_note if rep_note else out)

    def _prune_current_row(self):
        """「保存本行修改」在**没有改动可写**时的那一半：把当前这一条从报告里划掉。

        报告是待办清单，这个按钮是「本条办完了」。数据一个字节都不动，所以只在真写模式
        （非演练）下做——演练连盘都不碰，摘行就更是说谎；照样先问一声，摘下来的原文照旧
        进「已处理-<时间戳>.csv」，报告改写前的原文照旧进 patch-backup。
        """
        r = self._edit_row or {}
        key = self._edit_key or ("", "", "")
        try:
            idx = int(key[2])
        except (TypeError, ValueError):
            self.messagebox.showinfo("没定位到这一条",
                "这一条没有可用的「文件内序号」，没法从报告里摘掉。")
            return
        if self.dry_var.get():
            self.messagebox.showinfo("演练（不写盘）",
                "这一条没有要应用的修改；演练里盘上什么都不动，报告也不动，所以不摘行。\n"
                "要真的把它从报告里划掉，先取消「演练（不写盘）」。")
            return
        if not self.messagebox.askyesno("确认摘掉",
                "本条（%s#%s）没有要应用的修改，源 JSON 一个字节都不会改动。\n"
                "确定把这一条从报告里删掉吗？（原样抄进「已处理-<时间戳>.csv」留档，"
                "报告改写前的原文备份到 xcheck/patch-backup/）"
                % (r.get("文件", ""), r.get("文件内序号", ""))):
            return
        import time as _t
        n, note = self._prune_applied({(key[0], key[1], idx)},
                                      _t.strftime("%Y%m%d-%H%M%S"))
        if not n:
            self.messagebox.showinfo("没摘掉", "这一条没能在报告里定位到，未做任何改动。")
            return
        msg = "这一条没有要应用的修改，源 JSON 未改动。\n" + note
        left = self._pending_rows()
        if left:
            msg += ("\n工作表里还有 %d 条挂在账上（%d 处）→ 点「全部写回」一次落盘"
                    % (len(left), sum(c for _k, c in left)))
        self._update_save_buttons()
        self.set_status("已摘掉 1 行（本例未改数据）")
        self.messagebox.showinfo("完成", msg)

    def _write_and_report(self, plan, dry, export_only=False):
        t0 = self.root_dir and __import__("time").time()
        import time as _t
        ok = fail = 0
        files_done = 0
        written_vals = {}
        # 本轮**真正落地**的 (集合,文件,下标)：报告只按它记账。「保存本行修改」只写一条，
        # 别的行还挂着修改，绝不能跟着算「已修」。
        applied = set()
        if dry:
            # 演练不写盘，但报告得能看出「真写的话会消掉哪些」，所以按计划记账
            for (coll0, base0), edits0 in plan.items():
                for idx0, _f, _v in edits0:
                    applied.add((coll0, base0, idx0))
        for (coll, base), edits in sorted(plan.items()):
            path = resolve_path(self.S, coll, base)
            if not path:
                self.set_status("! 找不到文件 %s/%s" % (coll, base))
                fail += len(edits)
                continue
            data = json.loads(open(path, encoding="utf-8-sig").read())
            if isinstance(data, dict):
                data = [data]
            keep = []
            for idx, fld, val in edits:
                if data[idx].get(fld) == val:
                    written_vals[(coll, base, idx, fld)] = val
                    applied.add((coll, base, idx))     # 盘上已经是这个值，也算落地
                    continue
                # 字段名必须是这条记录里已有的，或者是集合自己声明的正文字段（允许给
                # 没有正文键的记录补上 paragraphs/content）——防止把人名敲错之类的键写进去
                if (fld in data[idx] or any(fld in d for d in data[:200])
                        or fld in self._body_fields):
                    keep.append((idx, fld, val))
                else:
                    fail += 1
            if not keep:
                continue
            res = patchjson.patch_file(path, keep, self.BACKUP, dry=dry)
            files_done += 1
            if res["ok"]:
                ok += len(keep)
                for idx, fld, val in keep:
                    written_vals[(coll, base, idx, fld)] = val
                    applied.add((coll, base, idx))
            else:
                fail += len(keep)
                self.set_status("! %s/%s: %s" % (coll, base, res["msg"]))
            self.root.update_idletasks()

        # 写过的文件缓存作废，否则比对区还显示写回前的内容
        sel = self.tree.selection()
        ri = self.iid_to_idx.get(sel[0]) if sel else None
        for (coll, base) in plan:
            p = self._path_cache.get((coll, base))
            if p:
                self._shard_cache.pop(p, None)
        # 这里**不再**逐条把判定归零、把作者/标题的比对基线挪到刚写下去的值上：那是「写过的
        # 行还留在工作表里」时的写法。现在落过盘的行等一下会被 _prune_applied 整行摘掉——
        # 行、判定、基线一起走，归零与否没人再看。演练（dry）本来就不走这段：盘上没变，
        # 判定就该原样挂着，提示栏也会照实说「还有 N 条挂在账上」。
        if ri is not None:
            try:
                # 面板照磁盘重读一遍：演练里没写盘，读到的是原样；非演练里落过盘的行马上会被
                # _prune_applied 整行摘掉，它自己会再刷一次，这一下只是给「行还在」兜底。
                self._refresh_compare(self.rows[ri])
                # 框里的文本还停在工作底稿上，拿重读后的原文再对一遍，免得判定与框里显示的
                # 对不上（比如盘上本来就是新值的那些，写了等于没写）。
                self._commit_current()
            except Exception:
                pass
        self._update_save_buttons()

        # 复核消除 + 导出
        stamp = _t.strftime("%Y%m%d-%H%M%S")
        kept_csv = os.path.join(self.WORK, "reconciled-%s.csv" % stamp)
        gone_csv = os.path.join(self.WORK, "eliminated-%s.csv" % stamp)
        kw = csv.writer(open(kept_csv, "w", encoding="utf-8-sig", newline=""))
        gw = csv.writer(open(gone_csv, "w", encoding="utf-8-sig", newline=""))
        hdr = ["集合", "文件", "文件内序号", "原标题", "原作者", "网站标题", "网站作者",
               "网站链接", "判定", "标题相似度", "正文相似度", "说明", "复核结果"]
        kw.writerow(hdr)
        gw.writerow(hdr)
        elim = Counter()
        kept = 0
        for r in self.rows:
            verdict = r.get("判定", "")
            new_a = (r.get("网站作者") or "").strip()
            new_t = (r.get("网站标题") or "").strip()
            old_a = (r.get("原作者") or "").strip()
            old_t = (r.get("原标题") or "").strip()
            try:
                idx = int(r.get("文件内序号", ""))
            except (ValueError, KeyError):
                idx = -1
            has_body = "正文" in verdict
            dec = self.decisions.get((r.get("集合", ""), r.get("文件", ""), r.get("文件内序号", "")), {})
            if (any(dec.get(f) is not None for f in ("author", "title", "body"))
                    and (r.get("集合", ""), r.get("文件", ""), idx) not in applied):
                # 这一行挂着修改，但本轮没写它（「保存本行修改」只管选中那一条，或者写盘
                # 失败）：报告里绝不能按「已修」记账——没落盘的修改等于没修。行照旧留在
                # 工作表里，判定也原样保留。
                r2 = dict(r)
                r2["复核结果"] = "未写回（本轮只写了选中的那一条）"
                kw.writerow([r2.get(h, "") for h in hdr])
                kept += 1
                continue
            da = dec.get("author")
            dt = dec.get("title")
            cur_a = written_vals.get((r.get("集合", ""), r.get("文件", ""), idx, "author"),
                                     old_a if da is None else da)
            cur_t = written_vals.get((r.get("集合", ""), r.get("文件", ""), idx, "title"),
                                     old_t if dt is None else dt)
            a_flag = bool(new_a) and new_a not in ANON and clean(old_a) != clean(new_a)
            t_flag = bool(new_t) and clean(old_t) != clean(new_t)
            a_ok = (not a_flag) or clean(cur_a) == clean(new_a)
            t_ok = (not t_flag) or clean(cur_t) == clean(new_t)
            # 正文不像标题/作者那样能离线复核：recheck 对正文差异一律留人工（ref.db 里的
            # 正文是列表页的截断预览，拿它当基准会把好行判成坏行）。所以这里只认一种情况
            # 「已消除」——本条正文的最终取值（写回的，或本来就是的）与**网上正文**归一
            # 后完全一致。网上正文没取到的（没联网/站点没这条/这行还没被选中过），报告里
            # 点了正文的就一律算待人工，宁可留着也不冤枉。
            b_key = (r.get("集合", ""), r.get("文件", ""), r.get("文件内序号", ""))
            meta = self._meta_by_key.get(b_key) or {}
            db_ = dec.get("body")
            b_cur = written_vals.get(
                (r.get("集合", ""), r.get("文件", ""), idx, meta.get("field") or ""),
                meta.get("orig", "") if db_ is None else db_)
            site_lines = self._site_lines_by_key.get(b_key)
            if not has_body:
                b_ok = True
            elif site_lines:
                bt = b_cur if isinstance(b_cur, (list, tuple)) else [b_cur]
                b_ok = clean("".join(str(x) for x in bt)) == clean("".join(site_lines))
            else:
                b_ok = False
            eliminated = a_ok and t_ok and b_ok
            note = []
            if a_flag and a_ok:
                note.append("作者已修")
            elif a_flag and not a_ok:
                note.append("作者需人工")
            if t_flag and t_ok:
                note.append("标题已修")
            elif t_flag and not t_ok:
                note.append("标题需人工")
            if has_body:
                note.append("正文已修" if b_ok else "正文待人工")
            if not note:
                note.append("已一致")
            r2 = dict(r)
            r2["复核结果"] = "；".join(note)
            if eliminated:
                gw.writerow([r2.get(h, "") for h in hdr])
                elim[verdict.split(":")[0] if ":" in verdict else verdict] += 1
            else:
                kw.writerow([r2.get(h, "") for h in hdr])
                kept += 1

        # 落了盘的行从报告里摘掉（演练一律不摘：盘上什么都没变）。放在导出之后——
        # reconciled/eliminated 记的是这一轮写盘本身（谁被写了、写成了什么），那些行还没从
        # self.rows 里消失，账才算得全。
        n_removed, prune_msg = (0, "") if dry else self._prune_applied(applied, stamp)
        mode = "（演练，未写盘）" if dry else "（已写盘）"
        msg = ("写回完成：涉及文件 %d，改写字段 %d，失败 %d，用时 %.0fs %s\n"
               "已消除 %d 条 → %s\n剩未消 %d 条 → %s"
               % (files_done, ok, fail, _t.time() - t0, mode,
                  sum(elim.values()), os.path.basename(gone_csv), kept, os.path.basename(kept_csv)))
        if prune_msg:
            msg += "\n" + prune_msg
        left = self._pending_rows()
        if left:
            # 只写了一条的时候必须说清「工作表里还剩多少」——不然用户以为全落盘了，
            # 而那一半的修改还在内存里，关窗口就没了。
            msg += ("\n工作表里还有 %d 条挂在账上（%d 处）→ 点「全部写回」一次落盘"
                    % (len(left), sum(c for _k, c in left)))
        self._update_save_buttons()
        self.set_status(msg.splitlines()[0] + ("；已摘掉 %d 行" % n_removed if n_removed else ""))
        self.messagebox.showinfo("完成", msg)

    # ---------------------------------------------------------- 杂项
    def open_link(self):
        url = self.detail.get("link", "")
        if url:
            import webbrowser
            webbrowser.open(url)

    def show_root(self):
        self.messagebox.showinfo("工程目录", "当前工程目录：\n%s" % self.root_dir)

    def set_status(self, text):
        try:
            self.status["text"] = text
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        _log_exc(traceback.format_exc())
        try:
            import tkinter as tk
            from tkinter import messagebox
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror("启动失败", "详见 reconcile_gui.log：\n" + traceback.format_exc()[-800:])
        except Exception:
            pass
