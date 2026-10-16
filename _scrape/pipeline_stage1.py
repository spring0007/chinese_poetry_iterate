# -*- coding: utf-8 -*-
"""古文岛采集 + 搜韵交叉核对管线（第一阶段：枚举与详情）。

输出:
  works_index.json  全部缺失朝代作品索引(古文岛)
  details.json      每首作品的详情(标题/作者/朝代/正文/URL)
"""
import urllib.request
import urllib.parse
import re
import json
import time
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.abspath(__file__))
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0',
      'Accept-Language': 'zh-CN,zh;q=0.9'}

DYNASTIES = ['两汉', '魏晋', '南北朝', '隋代', '金朝', '元代', '明代', '清代']


def fetch(url, retries=3, timeout=25):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8')
        except Exception as e:
            last = e
            time.sleep(0.8 * (i + 1))
    raise last


def parse_list_page(html):
    """从列表页解析作品: textarea 内含 正文——朝代·作者《标题》URL。"""
    out = []
    for m in re.finditer(r'<textarea[^>]*id="txtare\d+"[^>]*>(.*?)</textarea>', html, re.S):
        content = m.group(1).strip()
        t = re.search(r'——([^<《》]{1,40}?)《([^<《》]{1,60}?)》(https://www\.guwendao\.net/shiwenv_([a-z0-9]+)\.aspx)?\s*$', content)
        if not t:
            continue
        dyn_author, title, _, h = t.groups()
        if '·' in dyn_author:
            dyn, author = dyn_author.split('·', 1)
        else:
            dyn, author = dyn_author, ''
        out.append({'hash': h, 'dynasty': dyn.strip(), 'author': author.strip(), 'title': title.strip()})
    return out


def enumerate_dynasty(d):
    works = {}
    page = 1
    stale = 0
    while page <= 800:
        url = 'https://www.guwendao.net/shiwens/default.aspx?page=%d&cstr=%s' % (page, urllib.parse.quote(d))
        html = fetch(url)
        new = 0
        for it in parse_list_page(html):
            if it['hash'] and it['hash'] not in works:
                works[it['hash']] = it
                new += 1
        if new == 0:
            stale += 1
            if stale >= 2:
                break
        else:
            stale = 0
        page += 1
        time.sleep(0.05)
    return list(works.values())


def parse_detail(html):
    m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S)
    title = re.sub(r'<[^>]+>', '', m.group(1)).strip() if m else ''
    m = re.search(r'<p class="source">(.*?)</p>', html, re.S)
    author, dynasty = '', ''
    if m:
        src = m.group(1)
        am = re.search(r'<a[^>]*href="/authorv\.aspx\?name=[^"]*"[^>]*>(.*?)</a>', src, re.S)
        if am:
            txt = re.sub(r'<[^>]+>', '', am.group(1)).strip()
            lines = [l.strip() for l in txt.splitlines() if l.strip()]
            author = lines[-1] if lines else ''
        dm = re.search(r'cstr=([^"&]+)"[^>]*>〔([^〕]+)〕', src)
        if dm:
            dynasty = dm.group(2).strip()
    m = re.search(r'<div class="contson"[^>]*>(.*?)</div>', html, re.S)
    paragraphs = []
    if m:
        cont = m.group(1)
        # 统一换行：<br/> 与 </p> 转换行，<p> 起始标签去除
        cont = re.sub(r'<br\s*/?>', '\n', cont)
        cont = re.sub(r'</p>', '\n', cont)
        cont = re.sub(r'<p[^>]*>', '', cont)
        # 去除其余标签
        cont = re.sub(r'<[^>]+>', '', cont)
        # 实体与特殊字符
        cont = cont.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&#x3000;', '　')
        cont = cont.replace('&ldquo;', '“').replace('&rdquo;', '”').replace('&hellip;', '…').replace('&mdash;', '—')
        for line in cont.split('\n'):
            line = line.strip().strip('　').strip()
            # 去除可能残留的 /> 或 / 碎片
            line = re.sub(r'^/?>', '', line).strip()
            if line:
                paragraphs.append(line)
    return {'title': title, 'author': author, 'dynasty': dynasty, 'paragraphs': paragraphs}


def run(limit=None):
    # ---------- phase 1: enumerate ----------
    idx_path = os.path.join(BASE, 'works_index.json')
    if os.path.exists(idx_path):
        index = json.load(open(idx_path, encoding='utf-8'))
    else:
        index = {}
        for d in DYNASTIES:
            items = enumerate_dynasty(d)
            index[d] = items
            print('enum %s: %d' % (d, len(items)), flush=True)
        json.dump(index, open(idx_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    all_works = []
    for d, items in index.items():
        for it in items:
            it['site_dynasty'] = d
            all_works.append(it)
    print('total works:', len(all_works))

    # ---------- phase 2: details ----------
    det_path = os.path.join(BASE, 'details.json')
    details = {}
    if os.path.exists(det_path):
        details = json.load(open(det_path, encoding='utf-8'))
    todo = [w for w in all_works if w['hash'] not in details]
    if limit:
        todo = todo[:limit]
    print('details todo:', len(todo))

    def work(w):
        url = 'https://www.guwendao.net/shiwenv_%s.aspx' % w['hash']
        try:
            html = fetch(url)
            d = parse_detail(html)
            d['url'] = url
            d['hash'] = w['hash']
            d['site_dynasty'] = w['site_dynasty']
            d['site_author'] = w['author']
            d['site_title'] = w['title']
            return w['hash'], d
        except Exception as e:
            return w['hash'], {'error': str(e), 'hash': w['hash']}

    done = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(work, w): w for w in todo}
        for fut in as_completed(futs):
            h, d = fut.result()
            details[h] = d
            done += 1
            if done % 100 == 0:
                json.dump(details, open(det_path, 'w', encoding='utf-8'), ensure_ascii=False)
                print('details %d/%d' % (done, len(todo)), flush=True)
    json.dump(details, open(det_path, 'w', encoding='utf-8'), ensure_ascii=False)
    errs = [h for h, d in details.items() if 'error' in d]
    print('details done:', len(details), 'errors:', len(errs))


if __name__ == '__main__':
    lim = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run(lim)
