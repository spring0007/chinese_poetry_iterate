# -*- coding: utf-8 -*-
"""
Enrich the 2243 newly-added poems with guwendao (古文岛) attributes:
  译文(tr) / 注释(nt) / 赏析(ap) via AJAX, 创作背景(bg) via static HTML.

Key facts (verified):
  - uid MUST equal poetry-pipeline/build.py's make_uid so extras join poems.uid.
    uid = make_uid(code, norm_author(author), norm_text(title), "\n".join(norm_lines(paragraphs)))
    code ∈ {han,weijin,nanbeichao,sui,jin,yuan,ming,qing}
  - guwendao hash comes from _scrape/details.json, keyed by (norm_title, norm_author);
    18 collisions disambiguated by first line.
  - AJAX: https://www.guwendao.net/nocdn/ajaxshiwenDetailCont.aspx?id=<hash>&value=<yi|zhu|shang>
    headers Referer=shiwenv_<hash>.aspx, X-Requested-With: XMLHttpRequest
  - 创作背景 lives only in static HTML <h2>创作背景</h2> ... next <p>.

Writes into dist/extras/extras.jsonl via ExtrasStore (uid-keyed), source='guwendao'.
Resumable: progress file tracks per-uid per-field success; only missing fields are fetched.
"""
import json, os, sys, re, time, argparse, random
import urllib.request, urllib.parse

ROOT = "E:/chinese_poetry_iterate"
PP = os.path.join(ROOT, "poetry-pipeline")
sys.path.insert(0, PP)
import schema as S
from extras import ExtrasStore

DETAILS = os.path.join(ROOT, "_scrape/details.json")
EXTRAS_ROOT = os.path.join(PP, "dist", "extras")
PROGRESS = os.path.join(ROOT, "_scrape", "enrich_progress.json")

FILES = [  # (relpath, code, label)
    ("汉/poet.han.0.json", "han", "汉"),
    ("魏晋/poet.weijin.0.json", "weijin", "魏晋"),
    ("南北朝/poet.nanbeichao.0.json", "nanbeichao", "南北朝"),
    ("隋/poet.sui.0.json", "sui", "隋"),
    ("金/poet.jin.0.json", "jin", "金"),
    ("元诗/poet.yuanshi.0.json", "yuan", "元"),
    ("明/poet.ming.0.json", "ming", "明"),
    ("清诗/poet.qingshi.0.json", "qing", "清"),
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
FIELDS = ("tr", "nt", "ap", "bg")
DELAY = 0.30  # seconds between requests

# ------------------------------------------------------------ http
def http_get(url, referer=None, is_ajax=False, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if referer:
        req.add_header("Referer", referer)
    if is_ajax:
        req.add_header("X-Requested-With", "XMLHttpRequest")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def fetch_with_retry(url, referer=None, is_ajax=False, retries=5):
    last = ""
    for i in range(retries):
        try:
            return http_get(url, referer, is_ajax)
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if i + 1 < retries:
                time.sleep(3 * (i + 1) if e.code in (429, 503) else 1.5 * (i + 1))
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, str(e)[:80])
            if i + 1 < retries:
                time.sleep(1.5 * (i + 1))
    raise RuntimeError("fetch failed after %d tries: %s" % (retries, last))

# ------------------------------------------------------------ html parsing
def strip_tags(s):
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"</p>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()

def extract_contson(html):
    m = re.search(r'<div class="contson"[^>]*>(.*?)</div>\s*(?:<p[^>]*>\s*参考资料|</div>|$)', html, re.S)
    if m and m.group(1).strip():
        return m.group(1)
    # fallback: drop leading <h1>..</h1><p class="source">..</p> header, cut at 参考资料
    h = re.sub(r'^.*?</p>\s*', '', html, flags=re.S)
    h = h.split('参考资料')[0]
    return h

def parse_yi(html):
    con = extract_contson(html)
    spans = re.findall(r'<span style="color:#518564;">(.*?)</span>', con, re.S)
    out = [strip_tags(x) for x in spans]
    out = [x for x in out if x]
    return "\n".join(out)

def parse_zhu(html):
    con = extract_contson(html)
    out = []
    for p in re.findall(r'<p[^>]*>(.*?)</p>', con, re.S):
        parts = re.split(r'<br\s*/?>', p, maxsplit=1)
        if len(parts) > 1:
            t = strip_tags(parts[1])
            if t:
                out.append(t)
    return "\n".join(out)

def parse_shang(html):
    con = extract_contson(html)
    return strip_tags(con)

def parse_bg(html):
    m = re.search(r'创作背景</span></h2>(.*?)(?:<div class="sons"|<h2)', html, re.S)
    if not m:
        m = re.search(r'创作背景.*?</h2>(.*?)(?:<div class="sons"|<h2)', html, re.S)
    if not m:
        return ""
    return strip_tags(m.group(1))

def parse_shang_static(html):
    """赏析 from static page: pick the longest contson among 赏析 sections
    (handles the visible + hidden display:none duplicate)."""
    best = ""
    for seg in re.split(r'<h2', html):
        if '赏析' in seg[:80]:
            m = re.search(r'<div class="contson"[^>]*>(.*?)</div>', seg, re.S)
            if m:
                txt = strip_tags(m.group(1))
                if len(txt) > len(best):
                    best = txt
    return best

# ------------------------------------------------------------ build lookup
def build_details_index():
    details = json.load(open(DETAILS, encoding="utf-8"))
    idx = {}
    for h, v in details.items():
        t = S.norm_text(v.get("title", ""))
        a = S.norm_author(v.get("author", ""))
        paras = S.norm_lines(v.get("paragraphs", []))
        fl = paras[0] if paras else ""
        idx.setdefault((t, a), []).append({"hash": h, "fl": fl})
    return idx

def match_hash(det_idx, title_n, author_n, first_line_n):
    cands = det_idx.get((title_n, author_n))
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]["hash"]
    for c in cands:
        if c["fl"] and first_line_n and c["fl"][:20] == first_line_n[:20]:
            return c["hash"]
    return cands[0]["hash"]

# ------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max poems to process (0=all)")
    ap.add_argument("--delay", type=float, default=DELAY)
    args = ap.parse_args()

    det_idx = build_details_index()
    progress = {}
    if os.path.exists(PROGRESS):
        progress = json.load(open(PROGRESS, encoding="utf-8"))
    done = progress.get("done", {})

    store = ExtrasStore(EXTRAS_ROOT).load()
    total = 0
    skipped = 0
    fetched = 0
    field_hits = {f: 0 for f in FIELDS}
    field_needed = {f: 0 for f in FIELDS}
    t0 = time.time()

    poems = []
    for rel, code, label in FILES:
        data = json.load(open(os.path.join(ROOT, rel), encoding="utf-8"))
        for it in data:
            author = S.norm_author(it.get("author", ""))
            title = S.norm_text(it.get("title", ""))
            lines = S.norm_lines(it.get("paragraphs", []))
            body = "\n".join(lines)
            uid = S.make_uid(code, author, title, body)
            fl = lines[0] if lines else ""
            poems.append((uid, code, title, author, fl))
    total = len(poems)
    print("loaded %d poems from 8 files" % total)

    count = 0
    for uid, code, title, author, fl in poems:
        if args.limit and count >= args.limit:
            break
        count += 1
        rec = done.get(uid, {})
        needed = [f for f in FIELDS if not rec.get(f)]
        if not needed:
            skipped += 1
            continue
        for f in needed:
            field_needed[f] += 1
        h = match_hash(det_idx, title, author, fl)
        if not h:
            continue
        url = "https://www.guwendao.net/shiwenv_%s.aspx" % h
        got = {}
        # 创作背景(bg) 来自静态 HTML（完整，无截断）
        if "bg" in needed:
            try:
                html = fetch_with_retry(url, referer=url)
                time.sleep(args.delay * (0.5 + random.random()))
                got["bg"] = parse_bg(html)
            except Exception as e:
                print("  [bg err] %s: %s" % (uid, str(e)[:80]))
        # 译文(tr)/注释(nt)/赏析(ap) via AJAX —— 静态页这些段落被"展开阅读全文"截断，
        # AJAX 返回完整内容，故统一走 AJAX。
        ajax_map = {"tr": ("yi", parse_yi), "nt": ("zhu", parse_zhu), "ap": ("shang", parse_shang)}
        for f in needed:
            if f not in ajax_map:
                continue
            val, parser = ajax_map[f]
            try:
                txt = fetch_with_retry(
                    "https://www.guwendao.net/nocdn/ajaxshiwenDetailCont.aspx?id=%s&value=%s" % (h, val),
                    referer=url, is_ajax=True)
                time.sleep(args.delay * (0.5 + random.random()))
                got[f] = parser(txt)
            except Exception as e:
                print("  [%s err] %s: %s" % (f, uid, str(e)[:80]))

        # commit
        fields = {f: got[f] for f in FIELDS if got.get(f)}
        if fields:
            store.put(uid, fields, source="guwendao", url=url)
            fetched += 1
            for f in fields:
                field_hits[f] += 1
        # record progress (merge)
        merged = dict(rec)
        for f in FIELDS:
            if got.get(f):
                merged[f] = True
        merged["hash"] = h
        done[uid] = merged

        # periodic checkpoint: flush extras + progress so a crash only loses the last batch
        if count % 50 == 0:
            store.flush()
            progress["done"] = done
            progress["field_hits"] = field_hits
            progress["total"] = total
            progress["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            json.dump(progress, open(PROGRESS, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            print("  progress %d/%d  fetched=%d  hits=%s  elapsed=%.0fs"
                  % (count, total, fetched, field_hits, time.time() - t0))

    # flush extras + progress
    n = store.flush()
    progress["done"] = done
    progress["field_hits"] = field_hits
    progress["total"] = total
    progress["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    json.dump(progress, open(PROGRESS, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print("\n==== DONE ====")
    print("total poems:", total, "skipped(complete):", skipped, "fetched(this run):", fetched)
    print("extras.jsonl now has", n, "records")
    print("field hits (cumulative in progress):", field_hits)
    print("field needed (this run):", field_needed)

if __name__ == "__main__":
    main()
