# -*- coding: utf-8 -*-
"""第三阶段: 去重 + 交叉核对(仓库既有数据集/搜韵) + 作者小传 + 输出最终 JSON。

交叉核对策略(在搜韵不可用/限流时的可用方案):
  - 优先采用 search 站点(搜韵 sou-yun.cn)的结果(若已核实且为 diff_*, 记入 error.json)。
  - 以仓库内既有数据集作为独立交叉基准: 对 (作者,标题) 重叠的作品逐一比对正文,
      完全相同(相似度>=0.97) -> 视为重复, 不重复添加;
      明显差异(0.5<=相似度<0.97) -> 记入 error.json(附仓库原文与古文岛原文);
      无重叠 -> 以古文岛原文写入。
  - 作品内部再去重(同作者同标题/同正文)。

输出(仓库根目录):
  <朝代>/authors.<code>.json
  <朝代>/poet.<code>.0.json (每片 1000 条)
  <朝代>/error.json (有差异时)
  _scrape/dedup_report.json
"""
import urllib.request
import urllib.parse
import re
import json
import os
import time
import uuid
import difflib
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BASE)
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0',
      'Accept-Language': 'zh-CN,zh;q=0.9'}
MUA = {'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1'}

DYNASTY_MAP = {
    '两汉': ('汉', 'han'),
    '魏晋': ('魏晋', 'weijin'),
    '南北朝': ('南北朝', 'nanbeichao'),
    '隋代': ('隋', 'sui'),
    '金朝': ('金', 'jin'),
    '元代': ('元', 'yuanshi'),
    '明代': ('明', 'ming'),
    '清代': ('清', 'qingshi'),
}
FOLDER = {'汉': '汉', '魏晋': '魏晋', '南北朝': '南北朝', '隋': '隋', '金': '金',
          '元': '元诗', '明': '明', '清': '清诗'}

POETRY_DIRS = ['全唐诗', '宋词', '五代诗词', '诗经', '楚辞', '论语', '四书五经', '蒙学',
               '纳兰性德', '幽梦影', '御定全唐詩', '曹操诗集', '水墨唐诗', '近现代', '元曲']
DEFAULT_AUTHOR = {'曹操诗集': '曹操', '论语': '孔子', '四书五经': '佚名',
                  '幽梦影': '张潮', '诗经': '佚名', '蒙学': '佚名'}


def fetch(url, headers=UA, retries=3, timeout=25):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            return urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8')
        except Exception as e:
            last = e
            time.sleep(0.8 * (i + 1))
    raise last


def iter_records(obj):
    if isinstance(obj, list):
        for r in obj:
            yield r
    elif isinstance(obj, dict):
        yield obj


def extract_paragraphs(body):
    out = []
    def rec(x):
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, list):
            for y in x:
                rec(y)
        elif isinstance(x, dict):
            for k in ('paragraphs', 'content', 'text', 'body'):
                if k in x:
                    rec(x[k])
    rec(body)
    return out


def record_fields(r, folder):
    title = r.get('title') or r.get('chapter') or r.get('rhythmic') or ''
    author = r.get('author') or DEFAULT_AUTHOR.get(folder, '')
    body = None
    for k in ('paragraphs', 'content', 'para', 'body', 'text'):
        if k in r:
            body = r[k]
            break
    if isinstance(body, str):
        body = [body]
    return title, author, extract_paragraphs(body or [])


def norm_text(s):
    if not isinstance(s, str):
        s = str(s)
    s = re.sub(r'<[^>]+>', '', s or '')
    s = re.sub(r'[（(][^（）()]{0,40}?一作[^（）()]{0,40}?[）)]', '', s)
    s = re.sub(r'[（(][^（）()]{0,40}?集作[^（）()]{0,40}?[）)]', '', s)
    punct = r'[\s\u3000\u2000-\u200f，。！？；：、·…—－\-《》〈〉“”‘’「」『』（）()【】\[\]{}!,?;:"\'`~@#$%^&*_+=|\\/<>0-9０-９]'
    s = re.sub(punct, '', s)
    s = s.replace('〇', '零')
    return s


def norm_author(s):
    return re.sub(r'[\s\u3000]+', '', (s or ''))


def build_dedup_index():
    """既有数据集索引: (作者,规范标题)->[正文], 以及 (作者,正文)->True, 及来源样本。"""
    title_idx = {}
    content_idx = {}
    sample_idx = {}  # (na,nt) -> [(norm_content, folder)]
    for folder in POETRY_DIRS:
        d = os.path.join(REPO, folder)
        if not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for fn in files:
                if not fn.endswith('.json'):
                    continue
                fp = os.path.join(root, fn)
                try:
                    data = json.load(open(fp, encoding='utf-8'))
                except Exception:
                    continue
                rel = os.path.relpath(root, d)
                sub = folder if rel == '.' else folder + '/' + rel
                for r in iter_records(data):
                    if not isinstance(r, dict):
                        continue
                    title, author, body = record_fields(r, sub if sub in DEFAULT_AUTHOR else folder)
                    na, nt = norm_author(author), norm_text(title)
                    nc = norm_text(''.join(body))
                    if nt:
                        title_idx.setdefault((na, nt), []).append(nc)
                    if nc:
                        content_idx.setdefault((na, nc), True)
                        sample_idx.setdefault((na, nt), []).append((nc, sub))
    return title_idx, content_idx, sample_idx


def check_overlap(rec, title_idx, content_idx, sample_idx):
    """返回 (action, info)。action: skip(重复) / flag(差异) / add 。
    - 完全相同(>=0.97) 或 古文岛文本被仓库完整包含(仓库更全) -> skip
    - 真实异文(0.5<=相似度<0.97 且非包含关系) -> flag 记入 error.json
    - 无重叠 / 不同作品(相似度<0.5) -> add
    """
    na = norm_author(rec.get('author') or '')
    nt = norm_text(rec.get('title') or '')
    nc = norm_text(''.join(rec.get('paragraphs') or []))
    if not nc:
        return ('add', None)
    if (na, nc) in content_idx:
        return ('skip', 'exact_content')
    cands = sample_idx.get((na, nt))
    if cands:
        best, best_src, best_txt = 0.0, '', ''
        contain = 0.0
        for c, src in cands:
            if not c:
                continue
            sim = difflib.SequenceMatcher(None, nc, c).ratio()
            if sim > best:
                best, best_src, best_txt = sim, src, c
            #  containment: 较短文本被较长文本覆盖的比例
            shorter, longer = (nc, c) if len(nc) <= len(c) else (c, nc)
            if shorter:
                sm = difflib.SequenceMatcher(None, shorter, longer)
                matched = sum(b.size for b in sm.get_matching_blocks())
                contain = max(contain, matched / len(shorter))
        if best >= 0.97 or contain >= 0.97:
            return ('skip', 'exact_or_contained')
        if best >= 0.5:
            return ('flag', {'sim': round(best, 3), 'source': best_src, 'repo_text': best_txt})
    return ('add', None)


def get_author_bio(name):
    """古文岛作者小传: 桌面页取移动端链接, 移动页取正文简介。"""
    try:
        html = fetch('https://www.guwendao.net/authorv.aspx?name=' + urllib.parse.quote(name))
        m = re.search(r'm\.guwendao\.net/(authorv_[a-z0-9]+\.aspx)', html)
        if m:
            mhtml = fetch('https://m.guwendao.net/%s' % m.group(1), headers=MUA)
            pm = re.search(r'<p style="margin:0;">(.*?)</p>', mhtml, re.S)
            if pm:
                bio = re.sub(r'<br\s*/?>', '\n', pm.group(1))
                bio = re.sub(r'<[^>]+>', '', bio)
                bio = bio.replace('&nbsp;', ' ').replace('&amp;', '&&')
                bio = bio.replace('&amp;', '&')
                return '\n'.join(l.strip() for l in bio.split('\n') if l.strip())
        dm = re.search(r'<meta name="description" content="([^"]*)"', html)
        if dm:
            return dm.group(1).strip()
    except Exception:
        pass
    return ''


def run():
    details = json.load(open(os.path.join(BASE, 'details.json'), encoding='utf-8'))
    verify = json.load(open(os.path.join(BASE, 'verify.json'), encoding='utf-8')) if os.path.exists(
        os.path.join(BASE, 'verify.json')) else {}

    print('building cross-check index from existing datasets...')
    title_idx, content_idx, sample_idx = build_dedup_index()
    print('title keys:', len(title_idx), 'content keys:', len(content_idx))

    groups = {}
    dupes, errors = [], []
    souyun_diffs = 0
    repo_flags = 0
    for h, v in details.items():
        if 'error' in v:
            continue
        site_dyn = v.get('site_dynasty', '')
        if site_dyn not in DYNASTY_MAP:
            continue
        dyn, code = DYNASTY_MAP[site_dyn]
        rec = {'hash': h, 'dynasty': dyn, 'code': code,
               'title': v.get('title') or v.get('site_title') or '',
               'author': v.get('author') or v.get('site_author') or '',
               'paragraphs': v.get('paragraphs') or [],
               'url': v.get('url', '')}
        vr = verify.get(h, {})
        status = vr.get('status')
        if status in ('diff_title', 'diff_author', 'diff_content'):
            # 搜韵认定的差异
            best = vr.get('best') or {}
            rec['error'] = {
                'field': {'diff_title': 'title', 'diff_author': 'author',
                          'diff_content': 'content'}.get(status, status),
                'status': status,
                'note': vr.get('note', ''),
                'similarity': vr.get('similarity'),
                'guwendao': {'title': rec['title'], 'author': rec['author'],
                             'dynasty': dyn, 'paragraphs': rec['paragraphs'], 'url': rec['url']},
                'other_site': {'site': '搜韵 sou-yun.cn', 'title': best.get('title', ''),
                               'author': best.get('author', ''), 'dynasty': best.get('dynasty', ''),
                               'sentences': best.get('sentences', []), 'url': best.get('url', '')},
            }
            errors.append(rec)
            souyun_diffs += 1
            continue
        # 非差异 -> 仓库内交叉核对
        action, info = check_overlap(rec, title_idx, content_idx, sample_idx)
        if action == 'skip':
            rec['dupe_reason'] = info
            dupes.append(rec)
        elif action == 'flag':
            rec['error'] = {
                'field': 'content',
                'status': 'diff_content',
                'note': '与仓库既有数据集正文不一致(相似度 %.2f)' % info['sim'],
                'similarity': info['sim'],
                'guwendao': {'title': rec['title'], 'author': rec['author'],
                             'dynasty': dyn, 'paragraphs': rec['paragraphs'], 'url': rec['url']},
                'other_site': {'site': '仓库既有数据集', 'source': info['source'],
                               'norm_text': info['repo_text']},
            }
            errors.append(rec)
            repo_flags += 1
        else:
            groups.setdefault(dyn, []).append(rec)

    json.dump(dupes, open(os.path.join(BASE, 'dedup_report.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('repo-exact dupes removed:', len(dupes))
    print('souyun diffs:', souyun_diffs, '| repo diff flags:', repo_flags)
    for dyn, recs in sorted(groups.items()):
        print(' %s: %d verified' % (dyn, len(recs)))
    print('error entries:', len(errors))

    # 组内再去重(标题+内容)
    for dyn, recs in groups.items():
        seen_t, seen_c = set(), set()
        uniq = []
        for r in recs:
            kt = (norm_author(r['author']), norm_text(r['title']))
            kc = (norm_author(r['author']), norm_text(''.join(r['paragraphs'])))
            if kt in seen_t or kc in seen_c:
                r['dupe_reason'] = 'intra'
                dupes.append(r)
                continue
            seen_t.add(kt)
            seen_c.add(kc)
            uniq.append(r)
        groups[dyn] = uniq

    # 作者小传
    authors_needed = {}
    for dyn, recs in groups.items():
        for r in recs:
            authors_needed.setdefault(r['author'], dyn)
    print('authors needed:', len(authors_needed))
    apath = os.path.join(BASE, 'author_bios.json')
    bios = json.load(open(apath, encoding='utf-8')) if os.path.exists(apath) else {}

    def fa(item):
        name, dyn = item
        if name in bios:
            return name, bios[name]
        try:
            return name, get_author_bio(name)
        except Exception:
            return name, ''

    todo = [(n, d) for n, d in authors_needed.items() if n not in bios]
    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for name, bio in ex.map(fa, todo):
            bios[name] = bio
            done += 1
            if done % 50 == 0:
                json.dump(bios, open(apath, 'w', encoding='utf-8'), ensure_ascii=False)
                print('bios %d/%d' % (done, len(todo)), flush=True)
    json.dump(bios, open(apath, 'w', encoding='utf-8'), ensure_ascii=False)

    # 写输出
    errs_by_dyn = {}
    for e in errors:
        errs_by_dyn.setdefault(e['dynasty'], []).append(e)
    all_dyns = sorted({d for d, _ in DYNASTY_MAP.values()})
    for dyn in all_dyns:
        recs = groups.get(dyn, [])
        code = [v[1] for v in DYNASTY_MAP.values() if v[0] == dyn][0]
        folder = os.path.join(REPO, FOLDER[dyn])
        os.makedirs(folder, exist_ok=True)
        author_ids = {}
        authors_out = []
        for name in sorted({r['author'] for r in recs}):
            aid = str(uuid.uuid4())
            author_ids[name] = aid
            authors_out.append({'desc': bios.get(name, ''), 'name': name, 'id': aid, 'dynasty': dyn})
        with open(os.path.join(folder, 'authors.%s.json' % code), 'w', encoding='utf-8') as f:
            json.dump(authors_out, f, ensure_ascii=False, indent=1)
        recs_sorted = sorted(recs, key=lambda r: (r['author'], r['title']))
        for i in range(0, len(recs_sorted), 1000):
            shard = recs_sorted[i:i + 1000]
            out = [{'author': r['author'], 'paragraphs': r['paragraphs'], 'title': r['title'],
                    'id': str(uuid.uuid4()), 'dynasty': dyn,
                    'authorId': author_ids[r['author']]} for r in shard]
            fn = 'poet.%s.%d.json' % (code, i)
            with open(os.path.join(folder, fn), 'w', encoding='utf-8') as f:
                json.dump(out, f, ensure_ascii=False, indent=1)
        errs = errs_by_dyn.get(dyn, [])
        efile = os.path.join(folder, 'error.json')
        if errs:
            eout = [{'title': e['error']['guwendao']['title'], 'author': e['error']['guwendao']['author'],
                     'dynasty': dyn, **e['error']} for e in errs]
            with open(efile, 'w', encoding='utf-8') as f:
                json.dump(eout, f, ensure_ascii=False, indent=1)
        else:
            # 清空上一轮遗留的陈旧 error.json, 确保仅保留本轮真实差异
            if os.path.exists(efile):
                os.remove(efile)
        print('written %s: %d works, %d authors, %d errors' % (folder, len(recs), len(authors_out), len(errs)))


if __name__ == '__main__':
    run()
