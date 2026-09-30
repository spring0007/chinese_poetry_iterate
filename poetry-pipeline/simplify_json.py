# -*- coding: utf-8 -*-
"""
simplify_json.py —— 源库 JSON 就地繁转简，格式不变。

把源库（`sources.py` 里登记的那一批集合 + rank + strains）和扩展属性层
`dist/extras/extras.jsonl` 里的繁体字转成简体，写回**原文件**。

为什么不是"读出来 json.dump 回去"就算完：
所谓"保存原来的格式"，在这批文件上至少有 5 种互不相同的写法——4 空格缩进 LF、
2 空格缩进 LF、4 空格 CRLF、冒号前不留空格、行尾多一个空格……其中有几种
json.dumps 根本复现不出来（它不是合法 dump 结果的形状）。直接 dump 会让
diff 里混进成片的重排噪声，评审时看不出到底改了哪些字。

所以这里走三级策略：
  1. 先按"候选重序列化 == 原文字节"筛出当前文件的真实格式参数；
     能复现就用这套参数 dump 简体版本 —— 字节级对齐，diff 只有汉字变。
  2. 复现不了（说明原文件不是标准 dumps 产物）就降级成"逐字符替换"：
     只替换 zhconv 会改的单字，其它字节一动不动。
  3. 两种落盘前都要过校验：JSON 能解析 / 结构与原文完全一致 / 只有字符串变了。
     任一不过就拒绝写这个文件并报出来，绝不半写。

用法：
    python simplify_json.py                        # 体检（默认 dry-run，不落盘）
    python simplify_json.py --apply                # 落盘（自动备份被改的文件）
    python simplify_json.py --apply --include-optional   # 含「御定全唐詩」可选包
    python simplify_json.py --apply --backup-dir X:\bak  # 指定备份目录
    python simplify_json.py --apply --limit 20     # 只处理前 20 个文件（冒烟）
    python simplify_json.py --no-extras            # 不动 dist/extras
    python simplify_json.py --retry-verify         # 只做落盘后复查（是否已无繁体）
"""

from __future__ import annotations
import os, sys, json, csv, shutil, argparse, collections, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sources as SRC

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")

try:
    from zhconv import convert as _zh
except ImportError:
    sys.exit("缺少依赖：pip install zhconv==1.4.3")

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_EXTRAS = os.path.join(HERE, "dist", "extras", "extras.jsonl")


# ============================================================ 转换
_MAX_HOPS = 6


def conv(s: str) -> str:
    """单串繁转简，反复转到不再变化为止。

    为什么要循环（这是实测出来的坑）：zhconv 的转换链存在"多级跳转"，
    典型如 餘 → 馀 → 余，一次 convert() 只走一级。第一轮全量跑完仍有
    168 个文件、665 个字段残留繁体，第二遍才收敛。与其让使用者记着"要跑两遍"，
    不如在这里循环到不动点；跑满 _MAX_HOPS 还不停就按现状返回，由校验环节兜底。
    """
    out = s
    for _ in range(_MAX_HOPS):
        try:
            nxt = _zh(out, "zh-hans")
        except Exception:
            return out
        if nxt == out:
            break
        out = nxt
    return out


class Stat(object):
    def __init__(self):
        self.strings = 0      # 变动的字符串数
        self.keys = 0         # 变动的键名数
        self.pairs = collections.Counter()   # 形如 榘->矩 的合并字统计（按字段归类后上报）


def deep(node, st: Stat, field=""):
    if isinstance(node, str):
        nv = conv(node)
        if nv != node:
            st.strings += 1
            if len(node) == len(nv):
                for a, b in zip(node, nv):
                    if a != b:
                        st.pairs[(a, b)] += 1
        return nv
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            nk = conv(k) if isinstance(k, str) else k
            if isinstance(k, str) and nk != k:
                st.keys += 1
            out[nk] = deep(v, st, str(nk))
        return out
    if isinstance(node, list):
        return [deep(v, st, field) for v in node]
    return node


# ============================================================ 格式探测
def _guess_opts(sample: str):
    """按原文外观猜一组 json.dumps 参数，返回生成器（先最可能的）。"""
    ensure_ascii = "\\u" in sample[:20000]
    txt = sample
    # 缩进：第二个非空行首的空格数；是 '['/ '{' 紧接内容则视为无缩进
    lines = txt.replace("\r\n", "\n").split("\n")
    indent = None
    for ln in lines[1:6]:
        s = ln[: len(ln) - len(ln.lstrip())]
        if s:
            indent = "\t" if "\t" in s else len(s)
            break
    # 冒号分隔：": " 有空格 vs ":" 无空格；行尾逗号有无空格同理
    no_space = '":' in txt and '": ' not in txt
    sep_items = [None, (",", ":"), (", ", ": ")]
    indents = [indent, None, 4, 2, 1, 8, "\t"] if indent is not None else [None, 4, 2, 1, 8, "\t"]
    eas = [ensure_ascii, not ensure_ascii]
    if no_space:
        sep_items = [(None if indent else (",", ":")), (",", ":"), None, (", ", ": ")]
    for ea in eas:
        for ind in indents:
            for sp in sep_items:
                yield dict(ensure_ascii=ea, indent=ind, separators=sp)


def detect_format(data, norm_text: str):
    """找到能让「原数据」重序列化后字节等于原文的 dumps 参数；找不到返回 None。"""
    seen = set()
    for opts in _guess_opts(norm_text):
        key = (repr(opts["indent"]), opts["ensure_ascii"], repr(opts["separators"]))
        if key in seen:
            continue
        seen.add(key)
        try:
            if json.dumps(data, **opts) == norm_text:
                return opts
        except Exception:
            continue
    # 猜测失败就穷举一遍（慢，但只在少数文件上发生）
    for ea in (False, True):
        for ind in (None, 1, 2, 4, 8, "\t"):
            for sp in (None, (",", ":"), (", ", ": "), (",", ": ")):
                if (repr(ind), ea, repr(sp)) in seen:
                    continue
                seen.add((repr(ind), ea, repr(sp)))
                try:
                    if json.dumps(data, **opts_of(ea, ind, sp)) == norm_text:
                        return opts_of(ea, ind, sp)
                except Exception:
                    continue
    return None


def opts_of(ea, ind, sp):
    return dict(ensure_ascii=ea, indent=ind, separators=sp)


def split_eol(raw: bytes, enc: str):
    """
    拆成 (crlf, core, tail)。core 统一换成 LF 且不含结尾换行，tail 是结尾那几个换行。

    why：这批文件里 LF 和 CRLF 混着出现。若带着 \\r 去做"重序列化 == 原文"比对，
    CRLF 文件会永远比对不上而掉进保真兜底；若把 \\r 直接丢掉，写回去又换了行尾约定。
    所以：比对用 LF 归一过的 core，写回时再按 nl 还原。
    """
    text = raw.decode(enc, errors="replace")
    crlf = "\r\n" in text
    core = text.replace("\r\n", "\n")
    n = len(core) - len(core.rstrip("\n"))
    return ("\r\n" if crlf else "\n"), core[: len(core) - n] if n else core, "\n" * n


def join_eol(core: str, nl: str, tail: str) -> bytes:
    return core.replace("\n", nl).encode("utf-8") + tail.replace("\n", nl).encode("utf-8")


# ============================================================ 结构校验
def shape(node):
    """结构指纹：类型 + 键 + 非字符串标量。字符串内容不参与。"""
    if isinstance(node, dict):
        return ("d", tuple((k, shape(v)) for k, v in node.items()))
    if isinstance(node, list):
        return ("l", tuple(shape(v) for v in node))
    if isinstance(node, str):
        return "s"
    return (type(node).__name__, node)


# ============================================================ 单文件处理
_charmap = {}


def _map_ch(ch: str) -> str:
    v = _charmap.get(ch)
    if v is None:
        v = conv(ch)
        _charmap[ch] = v
    return v


def process_json(path: str, apply: bool, backup_root: str, log) -> dict:
    raw = open(path, "rb").read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    enc = "utf-8-sig" if bom else "utf-8"
    text = raw.decode(enc)
    data = json.loads(text)

    st = Stat()
    new_data = deep(data, st)
    info = dict(path=path, changed=0, mode="", bytes0=len(raw), bytes1=len(raw), err="")

    if st.strings == 0 and st.keys == 0:
        info["mode"] = "unchanged"
        return info

    nl, core, tail = split_eol(raw, enc)
    opts = detect_format(data, core)
    cand = []          # 逐级降级的候选正文：先追求"字节级最小改动"，再追求能过校验

    if opts is not None:
        cand.append(("json", json.dumps(new_data, **opts)))
    # 整段文本转换：原文件不是标准 dumps 形状时的第一兜底。
    # 比逐字符好在哪儿：zhconv 有词组级映射（著/着 之类按词决定），
    # 逐字符替换会漏掉这些，落盘后"再转一次还会变"，属于没转干净。
    cand.append(("text", conv(core)))
    # 逐字符替换：连整段转换都不稳定时的最后手段，只改繁简二字 .\n
    cand.append(("char", "".join(_map_ch(c) for c in core)))

    body = None
    mode = ""
    for tag, bc in cand:
        try:
            cand_data = json.loads(bc)
        except Exception as e:
            continue
        if shape(cand_data) != shape(data):
            continue
        if cand_data == new_data:
            body, mode = bc, tag
            break
        if json.loads(json.dumps(cand_data)) != cand_data:
            continue
        # 与逐串转换结果不同，但自身已稳定：接受，记为 partial
        if tag == "char":
            body, mode = bc, "char-partial"
            break
        if cand_data == deep(cand_data, Stat()):
            body, mode = bc, tag + "-partial"
            break
    if body is None:
        info["err"] = "三种方式都过不了校验（很可能是原文就含损坏编码），跳过"
        info["mode"] = "SKIP"
        return info

    out = (bom and b"\xef\xbb\xbf" or b"") + join_eol(body, nl, tail)

    # ---- 校验（三级：能解析 / 等于目标简体版本 / 结构与原文一致）
    try:
        re_text = out.decode("utf-8-sig" if bom else "utf-8")
        re_data = json.loads(re_text)
    except Exception as e:
        info["err"] = "写前校验失败（无法解析）：%s" % e
        info["mode"] = "SKIP"
        return info
    if shape(re_data) != shape(data):
        info["err"] = "写前校验失败（结构与原文不一致）"
        info["mode"] = "SKIP"
        return info
    if re_data != new_data:
        # 允许 -partial：与逐串转换结果略有出入，但自身已无繁体可转。
        # 差别来自 zhconv 的词组级映射，在整Text 级转换里是另一种等价写法。
        if re_data == deep(re_data, Stat()):
            mode += "!"          # 日志里带 ! 的就是"已转干净但与整串转换口径不同"
        else:
            info["err"] = "写前校验失败（仍有繁体未转）"
            info["mode"] = "SKIP"
            return info

    info["changed"] = st.strings + st.keys
    info["mode"] = mode
    info["bytes1"] = len(out)
    info["pairs"] = st.pairs

    if apply:
        if backup_root:
            rel = os.path.relpath(path, os.path.dirname(os.path.dirname(os.path.abspath(SRC.ROOT))))
            dst = os.path.join(backup_root, os.path.basename(path))
            i = 1
            while os.path.exists(dst):
                dst = os.path.join(backup_root, os.path.basename(path) + ".%d" % i)
                i += 1
            os.makedirs(backup_root, exist_ok=True)
            shutil.copy2(path, dst)
            info["backup"] = dst
        tmp = path + ".tmp-zhconv"
        with open(tmp, "wb") as f:
            f.write(out)
        os.replace(tmp, path)
        # 回读确认
        back = open(path, "rb").read()
        if back != out:
            info["err"] = "回读不等于写入内容！"
            info["mode"] = "FAIL"
    return info


def process_jsonl(path: str, apply: bool, backup_root: str, log) -> dict:
    """JSON Lines（每行一个 JSON 对象）：格式按首行探测，之后复用。

    extras.jsonl 是稀疏扩展层，一行一条，顺序与内容必须原样保留——
    所以这里按行处理而不是整文件 dumps，行号错位会让 id 对不上别的数据。
    """
    raw = open(path, "rb").read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    enc = "utf-8-sig" if bom else "utf-8"
    nl, core, tail = split_eol(raw, enc)
    lines = core.split("\n")

    info = dict(path=path, changed=0, mode="", bytes0=len(raw), bytes1=len(raw), err="")
    opts = None
    out_lines = []
    st = Stat()
    for ln in lines:
        if not ln.strip():
            out_lines.append(ln)
            continue
        obj = json.loads(ln)
        new_obj = deep(obj, st)
        if opts is None:
            opts = detect_format(obj, ln)
        if new_obj != obj:
            if opts is not None:
                body = json.dumps(new_obj, **opts)
                if json.loads(body) != new_obj:
                    info["err"] = "行序列化后回读不一致"
                    info["mode"] = "SKIP"
                    return info
            else:
                body = "".join(_map_ch(c) for c in ln)
                try:
                    if json.loads(body) != new_obj:
                        body = ln  # 一行里出现不可复现形状就原样保留，单独统计
                        info.setdefault("skipped_lines", 0)
                        info["skipped_lines"] += 1
                except Exception:
                    body = ln
            out_lines.append(body)
        else:
            out_lines.append(ln)

    info["changed"] = st.strings + st.keys
    info["mode"] = "jsonl"
    info["pairs"] = st.pairs
    if st.strings + st.keys == 0:
        info["mode"] = "unchanged"
        return info

    body = "\n".join(out_lines)
    out = (bom and b"\xef\xbb\xbf" or b"") + join_eol(body, nl, tail)

    # 校验：行数一致、逐行能解析、逐行结构与原文一致
    text_out = out.decode(enc).replace("\r\n", "\n")
    if tail:
        text_out = text_out[: -len(tail)]      # 去掉尾部的换行哨兵，两边口径才一致
    got = text_out.split("\n")
    if len(got) != len(lines):
        info["err"] = "行数变化（%d -> %d）" % (len(lines), len(got))
        info["mode"] = "SKIP"
        return info
    for a, b in zip(lines, got):
        oa, ob = json.loads(a), json.loads(b)
        if shape(oa) != shape(ob):
            info["err"] = "某行结构与原文不一致"
            info["mode"] = "SKIP"
            return info

    info["bytes1"] = len(out)
    if apply:
        if backup_root:
            os.makedirs(backup_root, exist_ok=True)
            shutil.copy2(path, os.path.join(backup_root, os.path.basename(path)))
        tmp = path + ".tmp-zhconv"
        open(tmp, "wb").write(out)
        os.replace(tmp, path)
        if open(path, "rb").read() != out:
            info["err"] = "回读不等于写入内容！"
            info["mode"] = "FAIL"
    return info


# ============================================================ 文件清单
def collect_files(include_optional: bool):
    files, groups = [], collections.OrderedDict()
    for c in SRC.COLLECTIONS:
        if c.external:
            continue
        if c.optional and not include_optional:
            continue
        fs = SRC.glob_shards(c.pattern)
        files += fs
        groups.setdefault(c.name, 0)
        groups[c.name] += len(fs)
    for name, pat in SRC.RANK_MAP.items():
        fs = SRC.glob_shards(pat)
        files += fs
        groups["rank/" + name] = len(fs)
    for name, pat in SRC.STRAIN_MAP.items():
        fs = SRC.glob_shards(pat)
        files += fs
        groups["strains/" + name] = len(fs)
    # 去重保序
    seen, uniq = set(), []
    for f in files:
        k = os.path.normpath(os.path.abspath(f))
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq, groups


# ============================================================ 复查
def verify_files(files):
    """复查：重新转一遍，还有变化就说明没转干净。jsonl 逐行算，json 整文件算。"""
    bad = []
    for p in files:
        try:
            raw = open(p, "rb").read().decode("utf-8-sig")
        except Exception as e:
            bad.append((p, "无法读取 %s" % e))
            continue
        try:
            chunks = [json.loads(l) for l in raw.split("\n") if l.strip()] \
                if p.endswith(".jsonl") else [json.loads(raw)]
        except Exception as e:
            bad.append((p, "无法解析 %s" % e))
            continue
        st = Stat()
        for chunk in chunks:
            deep(chunk, st)
        if st.strings or st.keys:
            bad.append((p, "仍有 %d 处繁体" % (st.strings + st.keys)))
    return bad


# ============================================================ 主流程
def main():
    ap = argparse.ArgumentParser(description="源库 JSON 就地繁转简（保持原格式）")
    ap.add_argument("--apply", action="store_true", help="真正落盘；不加则只体检")
    ap.add_argument("--include-optional", action="store_true", help="包含可选包（御定全唐詩）")
    ap.add_argument("--no-extras", action="store_true", help="不处理 dist/extras/extras.jsonl")
    ap.add_argument("--backup-dir", default="", help="备份目录，默认 ./backup/zhconv-<时间戳>")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个文件")
    ap.add_argument("--report", default="", help="把逐文件结果写成 CSV")
    ap.add_argument("--retry-verify", action="store_true", help="只复查是否已无繁体")
    args = ap.parse_args()

    print("源库根：%s" % SRC.ROOT)
    files, groups = collect_files(args.include_optional)
    if args.limit:
        files = files[: args.limit]
    print("待处理文件 %d 个（%s）" % (len(files), ", ".join("%s×%d" % (k, v) for k, v in groups.items())[:200]))

    if args.retry_verify:
        extras = []
        if not args.no_extras and os.path.exists(DEFAULT_EXTRAS):
            extras = [DEFAULT_EXTRAS]
        bad = verify_files(files + extras)
        print("\n复查：%d 个文件仍含繁体" % len(bad))
        for p, why in bad[:20]:
            print("   %s  %s" % (why, p))
        return 1 if bad else 0

    backup_root = args.backup_dir
    if args.apply and not backup_root:
        backup_root = os.path.join(HERE, "backup",
                                   "zhconv-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    if backup_root:
        print("备份目录：%s" % backup_root)

    results, failures, diffs = [], [], []
    total_changed = 0
    mode_cnt = collections.Counter()
    pairs = collections.Counter()

    print("\n=== 体检/转换 ===")
    for i, p in enumerate(files, 1):
        try:
            info = process_json(p, args.apply, backup_root, None)
        except Exception as e:
            failures.append((p, "异常：%s" % e))
            if i % 200 == 0:
                print("   ... %d/%d" % (i, len(files)))
            continue
        results.append(info)
        if info["mode"] in ("SKIP", "FAIL") or info["err"]:
            failures.append((p, info["err"] or info["mode"]))
        elif info["mode"] != "unchanged":
            mode_cnt[info["mode"]] += 1
            total_changed += info["changed"]
            for k, v in (info.get("pairs") or {}).items():
                pairs[k] += v
        if i % 200 == 0:
            print("   ... %d/%d" % (i, len(files)))

    if not args.no_extras and os.path.exists(DEFAULT_EXTRAS):
        try:
            info = process_jsonl(DEFAULT_EXTRAS, args.apply, backup_root, None)
            results.append(info)
            if info["err"]:
                failures.append((DEFAULT_EXTRAS, info["err"]))
            elif info["mode"] != "unchanged":
                mode_cnt[info["mode"]] += 1
                total_changed += info["changed"]
                for k, v in (info.get("pairs") or {}).items():
                    pairs[k] += v
        except Exception as e:
            failures.append((DEFAULT_EXTRAS, "异常：%s" % e))

    mpath = os.path.join(os.path.dirname(DEFAULT_EXTRAS), "manifest.json")
    if not args.no_extras and os.path.exists(mpath):
        try:
            info = process_json(mpath, args.apply, backup_root, None)
            results.append(info)
            if info["err"]:
                failures.append((mpath, info["err"]))
            elif info["mode"] != "unchanged":
                mode_cnt[info["mode"]] += 1
                total_changed += info["changed"]
        except Exception as e:
            failures.append((mpath, "异常：%s" % e))

    print("\n=== 汇总 ===")
    print("文件总数      %d" % len(files))
    print("有改动        %d" % sum(1 for r in results if r.get("mode") not in ("unchanged", "SKIP", "FAIL", "")))
    print("无需改动      %d" % sum(1 for r in results if r.get("mode") == "unchanged"))
    print("落盘方式       %s" % dict(mode_cnt))
    print("改动字段数    %d" % total_changed)
    print("失败/跳过     %d" % len(failures))
    for p, why in failures[:15]:
        print("   ! %s : %s" % (why, p))

    if pairs:
        print("\n=== 高频合并字 Top20（这些字在简体里被并成同一个，人名/地名请留意）===")
        for (a, b), n in pairs.most_common(20):
            print("   %s -> %s   ×%d" % (a, b, n))

    if args.report:
        with open(args.report, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["文件", "方式", "改动数", "原字节", "新字节", "错误"])
            for r in results:
                w.writerow([r["path"], r.get("mode", ""), r.get("changed", 0),
                            r.get("bytes0", ""), r.get("bytes1", ""), r.get("err", "")])
        print("\n明细已写入 %s" % args.report)

    if not args.apply:
        print("\n这是体检，未写入任何文件。确认无误后加 --apply 落盘。")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
