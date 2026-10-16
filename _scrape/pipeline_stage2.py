# -*- coding: utf-8 -*-
"""第二阶段: 去重 + 搜韵(sou-yun.cn)交叉核对。

输出:
  verify.json  每首作品的核对结果 {hash: {status, best, ...}}
  dedup_report.json  被去重剔除的作品清单
"""
import urllib.request
import urllib.parse
import re
import json
import time
import os
import sys
import threading
import difflib
import http.cookiejar
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BASE)
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0',
      'Accept-Language': 'zh-CN,zh;q=0.9'}

# ---------------- 归一化 ----------------

def strip_variants(s: str) -> str:
    """去掉 (X 一作：Y) / (集作Y) 等异文注释。"""
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r'[（(][^（）()]{0,40}?一作[^（）()]{0,40}?[）)]', '', s)
        s = re.sub(r'[（(][^（）()]{0,40}?集作[^（）()]{0,40}?[）)]', '', s)
        s = re.sub(r'[（(][^（）()]{0,40}?亦作[^（）()]{0,40}?[）)]', '', s)
        s = re.sub(r'[（(][^（）()]{0,40}?或作[^（）()]{0,40}?[）)]', '', s)
    return s

_PUNCT = re.compile(r'[\s\u3000\u2000-\u200f，。！？；：、·…—－\-《》〈〉“”‘’「」『』（）()【】\[\]{}!,?;:"\'`~@#$%^&*_+=|\\/<>0-9０-９]')

def norm_content(s: str) -> str:
    s = strip_variants(s)
    s = s.replace('〇', '零')
    return _PUNCT.sub('', s)

_NUM = '一二三四五六七八九十百千零〇两'

def norm_title(s: str) -> str:
    s = (s or '').strip()
    s = re.sub(r'^《|》$', '', s)
    s = strip_variants(s)
    s = re.sub(r'[\s\u3000]+', '', s)
    s = re.sub(r'[·・、，,。.．:：;；\-—_]+$', '', s)
    return s

def title_stem(s: str) -> str:
    """去掉尾部序号(其五/之七/五/①)得到词干, 用于别名匹配。"""
    t = norm_title(s)
    t = re.sub(r'[（(]?其[%s]+[）)]?$' % _NUM, '', t)
    t = re.sub(r'[（()]?之[%s]+[）)]?$' % _NUM, '', t)
    t = re.sub(r'[·・\s]?[%s]{1,4}$' % _NUM, '', t)
    return t

def norm_author(s: str) -> str:
    return re.sub(r'[\s\u3000]+', '', (s or ''))

def coarse_dynasty(d: str) -> str:
    d = d or ''
    for k in ['元末明初', '明末清初']:
        if k in d:
            return '明' if '元末明初' == k else '清'
    if '金末元初' in d:
        return '金'
    for key, val in [('先秦', '先秦'), ('秦', '先秦'), ('汉', '汉'), ('魏', '魏晋'), ('晋', '魏晋'),
                     ('三国', '魏晋'), ('南北朝', '南北朝'), ('隋', '隋'), ('唐', '唐'),
                     ('北宋', '宋'), ('南宋', '宋'), ('宋', '宋'), ('辽', '金'), ('金', '金'),
                     ('元', '元'), ('明', '明'), ('清', '清'), ('近现代', '近现代'), ('当代', '近现代')]:
        if key in d:
            return val
    for k in ['梁', '陈', '齐', '周', '北魏', '东魏', '西魏', '北齐', '北周', '南朝', '北朝']:
        if k in d:
            return '南北朝'
    return d

# ---------------- 搜韵会话 (全局限速 + 退避) ----------------

_RATE_LOCK = threading.Lock()
_LAST_REQ = [0.0]
_MIN_INTERVAL = 0.3  # 两次请求之间最小间隔(秒)


def _rate_limit():
    with _RATE_LOCK:
        now = time.time()
        wait = _MIN_INTERVAL - (now - _LAST_REQ[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_REQ[0] = time.time()


class SouyunSession:
    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self.opener.addheaders = list(UA.items())
        self.url = 'https://sou-yun.cn/QueryPoem.aspx'
        self.vs = self.vg = ''
        self._lock = threading.Lock()

    def _field(self, html, name):
        m = re.search(r'name="%s"[^>]*value="([^"]*)"' % name, html)
        return m.group(1) if m else ''

    def ensure_viewstate(self):
        last_err = None
        for attempt in range(2):
            try:
                _rate_limit()
                html = self.opener.open(self.url, timeout=25).read().decode('utf-8')
                self.vs = self._field(html, '__VIEWSTATE')
                self.vg = self._field(html, '__VIEWSTATEGENERATOR')
                if self.vs:
                    return True
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 429:
                    time.sleep(1.5)
                    continue
                time.sleep(1.5)
            except Exception as e:
                last_err = e
                time.sleep(1.5)
        raise RuntimeError('cannot get viewstate: %s' % last_err)

    def search(self, kw='', author='', precise=True):
        last_err = None
        for attempt in range(2):
            try:
                with self._lock:
                    if not self.vs:
                        self.ensure_viewstate()
                    vs, vg = self.vs, self.vg
                _rate_limit()
                data = {'__VIEWSTATE': vs, '__VIEWSTATEGENERATOR': vg,
                        '_TitleKeys': '', '_ContentKeys': kw,
                        'KeywordTextBox': kw, 'QueryButton': '检索',
                        'SearchScope': '-1', 'IsPreciseMatch': 'on' if precise else '',
                        'ShowMatchedClauseOnly': '', 'AuthorKeyword': author,
                        'DynastyOption': '0', 'PTypeOption': 'All',
                        'RhymeCategory': '', 'StartInClause': ''}
                req = urllib.request.Request(self.url, data=urllib.parse.urlencode(data).encode(),
                                             headers={'Content-Type': 'application/x-www-form-urlencoded',
                                                      'Referer': self.url})
                return self.opener.open(req, timeout=30).read().decode('utf-8')
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 429:
                    time.sleep(1.5)
                    try:
                        self.ensure_viewstate()
                    except Exception:
                        pass
                    continue
                time.sleep(1.5)
            except Exception as e:
                last_err = e
                time.sleep(1.5)
        raise RuntimeError('search failed: %s' % last_err)


def parse_souyun(html):
    """解析搜韵检索结果, 返回候选列表。"""
    # 去掉所有空 div(注释占位), 避免截断正文
    cleaned = re.sub(r'<div[^>]*></div>', '', html)
    out = []
    blocks = re.split(r"<div id='item_\d+'><div id='poem_\d+' class='_poem'>", cleaned)
    for block in blocks[1:]:
        pid_m = re.match(r"(\d+)'", block)
        pid = pid_m.group(1) if pid_m else None
        tm = re.search(r"<a href='/Query\.aspx\?type=poem1&id=%s'[^>]*>(.*?)</a>" % (pid or r'\d+'), block, re.S)
        if not tm:
            continue
        title = re.sub(r'<[^>]+>', '', tm.group(1)).strip()
        am = re.search(r"<span class='poemAuthor'><span class='inlineComment1'>\s*([^<]*?)\s*·\s*</span><a[^>]*>([^<]+)</a>", block)
        dyn, author = (am.group(1).strip(), am.group(2).strip()) if am else ('', '')
        pm = re.search(r"<div class='titleComment' id='poem_preface_%s'>(.*?)</div>" % (pid or r'\d+'), block, re.S)
        preface = re.sub(r'<[^>]+>', '', pm.group(1)).strip() if pm else ''
        sents = re.findall(r"<div class='(?:poemSentence|articleSentence)'[^>]*>(.*?)</div>", block, re.S)
        sents = [re.sub(r'<[^>]+>', '', s).strip() for s in sents]
        sents = [s for s in sents if s]
        out.append({'pid': pid, 'title': title, 'dynasty': dyn, 'author': author,
                    'preface': preface, 'sentences': sents,
                    'url': 'https://sou-yun.cn/Query.aspx?type=poem1&id=%s' % pid})
    return out


def content_sim(paras, cand):
    """古文岛正文 vs 搜韵候选正文, 返回归一化后的相似度(0-1)。"""
    if not paras or not cand['sentences']:
        return 0.0
    ours = norm_content(''.join(paras))
    theirs_full = norm_content((cand.get('preface') or '') + ''.join(cand['sentences']))
    theirs_body = norm_content(''.join(cand['sentences']))
    if not ours:
        return 0.0
    best = 0.0
    for t in (theirs_full, theirs_body):
        if t:
            best = max(best, difflib.SequenceMatcher(None, ours, t).ratio())
    return best


def verify_work(sess, work):
    title = work.get('title') or ''
    author = work.get('author') or ''
    paras = work.get('paragraphs') or []
    ntitle, nauthor = norm_title(title), norm_author(author)
    stem = title_stem(title)
    # 非个人署名(佚名/乐府/诗经等): 视为匿名, 仅按标题检索与匹配
    COLLECTIVE = ('佚名', '无名氏', '无名', '汉乐府', '乐府', '乐府诗集', '诗经', '楚辞',
                  '古诗十九首', '玉台新咏', '玉台', '古乐府', '杂曲歌辞', '相和歌辞',
                  '鼓吹曲辞', '横吹曲辞', '清商曲辞', '近代曲辞', '杂歌谣辞')
    anon = (author in COLLECTIVE) or (author == '')

    # 检索: 先按标题检索(覆盖最广); 若未命中候选, 再补 (标题,作者)。佚名类仅标题检索。
    candidates = {}
    try:
        html = sess.search(kw=title, author='', precise=True)
        for c in parse_souyun(html):
            candidates[c['pid']] = c
    except Exception as e:
        return {'status': 'error', 'note': 'search failed: %s' % e}
    if not candidates and not anon:
        # 仅当标题检索无候选时, 再按 (标题,作者) 检索
        try:
            html = sess.search(kw=title, author=author, precise=True)
            for c in parse_souyun(html):
                candidates[c['pid']] = c
        except Exception as e:
            return {'status': 'error', 'note': 'search failed: %s' % e}

    if not candidates:
        return {'status': 'not_found', 'note': '搜韵未检索到相关作品(覆盖缺口, 按古文岛原文写入)'}

    def tmatch(c):
        ct = norm_title(c['title'])
        return ct == ntitle or (stem and (ct == stem or title_stem(c['title']) == stem))

    # 评分: (作者匹配, 标题匹配, 相似度, 候选)
    scored = []
    for c in candidates.values():
        am = anon or norm_author(c['author']) == nauthor or norm_author(c['author']) in COLLECTIVE
        tm = tmatch(c)
        sim = content_sim(paras, c)
        scored.append((am, tm, sim, c))

    OK_THRESHOLD = 0.97

    # 1) 同作者+同标题: 以最高相似度定夺
    same_work = [x for x in scored if x[0] and x[1]]
    if same_work:
        best = max(same_work, key=lambda x: x[2])
        if best[2] >= OK_THRESHOLD:
            return {'status': 'ok', 'best': best[3], 'similarity': round(best[2], 3)}
        return {'status': 'diff_content', 'best': best[3],
                'similarity': round(best[2], 3),
                'note': '同作者同标题但正文不一致(相似度 %.2f)' % best[2]}

    # 2) 同标题+正文一致(相似度高)但作者不同 -> diff_author
    tm_only = [x for x in scored if x[1] and not x[0]]
    if tm_only:
        best_t = max(tm_only, key=lambda x: x[2])
        if best_t[2] >= OK_THRESHOLD:
            return {'status': 'diff_author', 'best': best_t[3],
                    'note': '标题一致但作者不一致: 古文岛 %s / 搜韵 %s' % (author, best_t[3]['author'])}

    # 3) 同作者+正文一致但标题不同(别名) -> diff_title
    am_only = [x for x in scored if x[0] and not x[1]]
    if am_only:
        best_a = max(am_only, key=lambda x: x[2])
        if best_a[2] >= OK_THRESHOLD:
            return {'status': 'diff_title', 'best': best_a[3],
                    'note': '作者一致但标题不一致: 古文岛《%s》 / 搜韵《%s》' % (title, best_a[3]['title'])}

    # 其余: 他站虽有同名/同作者条目但无法确认是同一作品 -> 视为覆盖缺口, 不报错
    return {'status': 'not_found', 'note': '搜韵存在相关条目但无法确认同一作品, 按古文岛原文写入'}


def run(limit=None):
    details = json.load(open(os.path.join(BASE, 'details.json'), encoding='utf-8'))
    works = [{'hash': h, **v} for h, v in details.items() if 'error' not in v]
    if limit:
        works = works[:limit]
    print('to verify:', len(works))

    vpath = os.path.join(BASE, 'verify.json')
    verify = json.load(open(vpath, encoding='utf-8')) if os.path.exists(vpath) else {}
    todo = [w for w in works if w['hash'] not in verify or verify[w['hash']].get('status') == 'error']
    print('verify todo:', len(todo))

    tl = threading.local()

    def get_sess():
        if not hasattr(tl, 'sess'):
            s = SouyunSession()
            s.ensure_viewstate()
            tl.sess = s
        return tl.sess

    def work(w):
        for attempt in range(2):
            try:
                s = get_sess()
                r = verify_work(s, w)
                if r.get('status') != 'error':
                    return w['hash'], r
            except Exception as e:
                r = {'status': 'error', 'note': str(e)}
            # 会话可能失效, 刷新 viewstate 后重试一次
            try:
                s = get_sess()
                s.ensure_viewstate()
            except Exception:
                pass
        return w['hash'], r

    done = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(work, w) for w in todo]
        for fut in as_completed(futs):
            h, r = fut.result()
            verify[h] = r
            done += 1
            if done % 100 == 0:
                json.dump(verify, open(vpath, 'w', encoding='utf-8'), ensure_ascii=False)
                print('verified %d/%d' % (done, len(todo)), flush=True)
    json.dump(verify, open(vpath, 'w', encoding='utf-8'), ensure_ascii=False)

    from collections import Counter
    cnt = Counter(v['status'] for v in verify.values())
    print('verify stats:', dict(cnt))


if __name__ == '__main__':
    lim = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run(lim)
