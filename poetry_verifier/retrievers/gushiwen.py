# -*- coding: utf-8 -*-
"""古文岛（古诗文网，同源）检索实现。

已验证可用的路径（本环境实测）：
- 作者诗列表：https://www.gushiwen.cn/shiwens/default.aspx?astr=<作者>&p=N  （每页 ~30 首，可翻页）
- 诗篇页：    https://www.gushiwen.cn/shiwenv_<id>.aspx
  - 标题：<h1 ...>...</h1>
  - 朝代：<a href="/shiwens/default.aspx?cstr=X">〔X〕</a>
  - 作者：<a href="/authorv.aspx?name=...">NAME</a>
  - 正文：<div class="contson" id="contsonXXXX">...</div>

注意：古文岛的站内搜索（search.aspx）已登录墙，无法程序化搜索；
因此这里用“作者诗列表 + 标题匹配”定位诗篇，作为单一权威参考。
（古诗文网与古文岛同源，二者不能互相校对。）
"""
from __future__ import annotations

import difflib
import re
from typing import List, Optional, Tuple

from .base import HttpClient, Reference
from .. import normalize as N


class GushiwenRetriever:
    BASE = "https://www.gushiwen.cn"
    NAME = "古文岛(gushiwen.cn)"

    def __init__(self, http: HttpClient, max_pages: int = 4, title_match_ratio: float = 0.82):
        self.http = http
        self.max_pages = max_pages
        self.title_match_ratio = title_match_ratio

    # ---------- 解析 ----------
    @staticmethod
    def _strip_tags(s: str) -> str:
        s = re.sub(r"<[^>]+>", "", s)
        s = re.sub(r"&nbsp;", " ", s)
        s = re.sub(r"\s+", " ", s)
        return s.strip()

    def _parse_author_list(self, html: str) -> List[Tuple[str, str]]:
        """返回 [(shiwenv_id, 标题(转繁体))] 。"""
        out = []
        seen = set()
        # 抓取所有 shiwenv 链接及紧随其后的锚文本
        for m in re.finditer(r'href="(/shiwenv_[a-f0-9]+\.aspx)"[^>]*>(.*?)</a>', html, re.S):
            hexid = re.search(r"shiwenv_([a-f0-9]+)", m.group(1))
            if not hexid:
                continue
            sid = hexid.group(1)
            raw_title = self._strip_tags(m.group(2))
            if not raw_title or sid in seen:
                continue
            seen.add(sid)
            out.append((sid, N.to_traditional(raw_title)))
        return out

    def _parse_poem(self, html: str) -> Optional[dict]:
        if not html or "contson" not in html:
            return None
        # 标题
        h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
        title = self._strip_tags(h1.group(1)) if h1 else None
        if title:
            title = N.to_traditional(title)
        # 朝代
        dyn = re.search(r"default\.aspx\?cstr=([^>\"]+)\">〔([^〕]+)〕", html)
        dynasty = N.to_traditional(dyn.group(2)) if dyn else None
        # 作者
        au = re.search(r'href="/authorv\.aspx\?name=[^"]+"[^>]*>([^<]+)</a>', html)
        author = N.to_traditional(self._strip_tags(au.group(1))) if au else None
        # 正文
        block = re.search(r'<div class="contson"[^>]*>(.*?)</div>', html, re.S)
        raw = block.group(1) if block else ""
        # 剔除古文岛夹注（如 "(生处 一作：深处)"、"(斜阳 一作：夕阳)"）——它们是校勘注释，
        # 不是正文；若不剔除会被当作诗句写回语料（曾污染刘长卿《送灵澈》等）。
        raw = re.sub(r"[（(][^（）()]{0,40}一作[^（）()]{0,40}[）)]", "", raw)
        # 拆分 verse
        t = re.sub(r"<br\s*/?>", "\n", raw)
        t = re.sub(r"</p>", "\n", t)
        t = self._strip_tags(t)
        lines = N.split_verses(t)
        return {
            "title": title,
            "author": author,
            "dynasty": dynasty,
            "content_lines": lines,
            "raw_content": t,
        }

    # ---------- 获取 ----------
    def get_author_list(self, author_simplified: str) -> List[Tuple[str, str]]:
        out = []
        for p in range(1, self.max_pages + 1):
            url = f"{self.BASE}/shiwens/default.aspx?astr={author_simplified}&p={p}"
            html = self.http.fetch(url, cache_key=f"gw_list:{author_simplified}:p{p}")
            if not html or "contson" not in html and "shiwenv_" not in html:
                # 可能是登录墙或空页 → 停止翻页
                if p == 1:
                    break
            items = self._parse_author_list(html or "")
            if not items:
                break
            out.extend(items)
        return out

    def get_poem(self, shiwenv_id: str) -> Optional[dict]:
        url = f"{self.BASE}/shiwenv_{shiwenv_id}.aspx"
        html = self.http.fetch(url, cache_key=f"gw_poem:{shiwenv_id}")
        return self._parse_poem(html or "")

    # ---------- 定位 ----------
    def _match_title_all(self, local_title: str, candidates: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
        """返回所有与本地标题/词牌“命中”的候选 (sid, title)，按相似度降序。

        命中判定与旧 _match_title 一致（完全相等 1.0；包含/前缀 0.83+；否则序列相似度），
        但此处收集“全部”命中项，供同名/同词牌多首时做首句对齐消歧，而非盲目取第一个。
        """
        lt = N.to_comparable(local_title or "")
        if not lt:
            return []
        out = []
        seen = set()
        for sid, title in candidates:
            if sid in seen:
                continue
            rt = N.to_comparable(title)
            if not rt:
                continue
            if rt == lt:
                ratio = 1.0
            elif rt.startswith(lt) or lt.startswith(rt) or lt in rt or rt in lt:
                ratio = max(0.83, len(lt) / max(len(rt), len(lt)))
            else:
                ratio = difflib.SequenceMatcher(None, lt, rt).ratio()
            if ratio >= self.title_match_ratio:
                out.append((sid, title, ratio))
                seen.add(sid)
        out.sort(key=lambda x: x[2], reverse=True)
        return [(sid, title) for sid, title, _ in out]

    @staticmethod
    def _first_line_of(poem: dict) -> Optional[str]:
        """取诗篇正文第一条可比对且长度≥4 的行（用于首句对齐）。"""
        for ln in poem.get("content_lines", []) or []:
            c = N.to_comparable(ln)
            if len(c) >= 4:
                return c
        return None

    def _disambiguate_by_first_line(self, local_lines, matched: List[Tuple[str, str]]) -> Optional[str]:
        """当同名/同词牌有多首候选时，用本地首句对齐到正确的那首。

        返回选中的 sid；若无法可靠区分（首句相似度过低或优劣难分）返回 None，
        调用方据此判为“无可靠参考”，绝不用另一首词的正文去覆盖正确的本地词。
        """
        local_first = None
        if local_lines:
            for ln in local_lines:
                c = N.to_comparable(ln)
                if len(c) >= 4:
                    local_first = c
                    break
        if not local_first:
            return None
        scored = []
        for sid, _title in matched:
            try:
                poem = self.get_poem(sid)
            except Exception:
                continue
            if not poem:
                continue
            gf = self._first_line_of(poem)
            if not gf:
                continue
            # 合页（多首同页）时本地作品可能不在页面首行：取“本地首句 vs 页面
            # 任意一行”的最佳相似度，避免把正确的合页候选误拒。
            best = difflib.SequenceMatcher(None, local_first, gf).ratio()
            for ln in poem.get("content_lines", []) or []:
                c = N.to_comparable(ln)
                if len(c) >= 4:
                    best = max(best, difflib.SequenceMatcher(None, local_first, c).ratio())
            scored.append((best, sid))
        if not scored:
            return None
        scored.sort(reverse=True)
        best_sim, best_sid = scored[0]
        # 阈值 0.6：本地(潘阆)实测清晰匹配 0.96/0.96/0.62，非匹配项均 ≤0.4
        if best_sim < 0.6:
            return None
        # 明显最优：与次优差距 ≥0.1，避免两首首句相近时误判
        if len(scored) >= 2 and (scored[0][0] - scored[1][0]) < 0.1:
            return None
        return best_sid

    def _build_reference(self, sid: str, local_content_lines: Optional[List[str]] = None) -> Optional[Reference]:
        poem = self.get_poem(sid)
        if not poem:
            return None
        lines = poem.get("content_lines", [])
        note = None
        if local_content_lines:
            # 古文岛常把同词牌/同题的多首作品放在同一页（如皇甫松《摘得新》二首、
            # 《天仙子》二首）。若不截取，会把整页内容当作一首的参考，导致覆盖时
            # 把本地正确的词与另一首合并/顶替。此处按本地首句/末句截取本地对应片段。
            span, note = self._extract_local_span(local_content_lines, lines)
            if span is None:
                return None  # 无法可靠定位本地作品 → 无可靠参考
            lines = span
        return Reference(
            source_name=self.NAME,
            url=f"{self.BASE}/shiwenv_{sid}.aspx",
            title=poem.get("title"),
            author=poem.get("author"),
            dynasty=poem.get("dynasty"),
            content_lines=lines,
            raw_content="\n".join(lines),
        )

    @staticmethod
    def _sub_lines(lines) -> List[str]:
        """取可用于比对的行（去标点/空白，跳过过短行）。"""
        out = []
        for ln in lines or []:
            c = N.to_comparable(ln)
            if len(c) >= 4:
                out.append(c)
        return out

    def _extract_local_span(self, local_lines, ref_lines):
        """多首合页时截取本地作品对应的片段。

        返回 (截取后的行列表, 说明)；无法可靠定位时返回 (None, 原因)，调用方据此
        判为“无可靠参考”，绝不用另一首作品的正文去覆盖正确的本地作品。
        单首页面原样返回（允许异文：首句相似度 ≥0.8 且前三字相同）。
        """
        lsub = self._sub_lines(local_lines)
        if not lsub:
            return list(ref_lines), None
        lf, ll = lsub[0], lsub[-1]
        rsub = [N.to_comparable(x) for x in ref_lines]
        # 1) 定位本地首句所在行（取页面内最佳匹配；容忍首句讹字/异文，如“远目/远上”）
        start_idx, start_sim = None, 0.0
        for i, rc in enumerate(rsub):
            if not rc or rc[:1] != lf[:1]:
                continue
            sim = difflib.SequenceMatcher(None, lf, rc[:len(lf) + 2]).ratio()
            if sim > start_sim:
                start_idx, start_sim = i, sim
        if start_idx is None or start_sim < 0.75:
            return None, "本地首句无法在权威页面定位"
        # 2) 定位本地末句所在行（在 start_idx 及之后，倒序找最靠后的匹配）
        end_idx = None
        for j in range(len(rsub) - 1, start_idx - 1, -1):
            rc = rsub[j]
            if not rc:
                continue
            if ll in rc or difflib.SequenceMatcher(None, ll, rc).ratio() >= 0.85:
                end_idx = j
                break
        foreign_prefix = "".join(rsub[:start_idx])
        tail_after = "".join(rsub[(end_idx + 1):]) if end_idx is not None else ""
        note = "权威页面含多首作品，已截取本地对应片段"
        if start_idx == 0 and len(foreign_prefix) == 0 and (end_idx is None or len(tail_after) < 8):
            # 页面即本首（本地更长或末句未定位时按原样用，交给冲突/裁判路径）
            return list(ref_lines), None
        if start_idx == 0 and end_idx is not None and len(tail_after) >= 8:
            # 本地是页面第一首、其后还跟着别的首 → 截到本地末句，绝不并入后续首
            return ref_lines[:end_idx + 1], note
        if end_idx is None or end_idx < start_idx:
            return None, "本地末句无法在权威页面定位"
        # 本地首句前有别的首 → 截取 [start_idx, end_idx]
        return ref_lines[start_idx:end_idx + 1], note

    def resolve(self, local_title: Optional[str], local_author: Optional[str],
                local_rhythmic: Optional[str] = None,
                local_content_lines: Optional[List[str]] = None) -> Optional[Reference]:
        """根据（作者 + 标题/词牌）定位并返回 Reference。

        同名/同词牌多首（如潘阆十首《酒泉子》、柳永多首《少年游》）时，
        用本地首句对齐消歧，定位到正确的那一首；无法可靠区分则判无可靠参考。
        """
        if not local_author:
            return None  # 无作者 corpus 走 search_api（见 search_api.py）
        author_q = N.to_simplified(local_author)
        try:
            candidates = self.get_author_list(author_q)
        except Exception:
            return None
        if not candidates:
            return None
        # 先按标题，再按词牌，收集“全部”命中候选
        matched = self._match_title_all(local_title or "", candidates)
        if (not matched) and local_rhythmic:
            matched = self._match_title_all(local_rhythmic, candidates)
        if not matched:
            return None
        # 唯一命中 → 直接取；多个命中 → 首句对齐消歧
        if len(matched) == 1:
            return self._build_reference(matched[0][0], local_content_lines)
        chosen = self._disambiguate_by_first_line(local_content_lines, matched)
        if chosen is None:
            return None
        return self._build_reference(chosen, local_content_lines)

    def healthcheck(self) -> Tuple[bool, str]:
        html = self.http.fetch(self.BASE + "/", use_cache=False)
        if html and ("古诗文网" in html or "古文岛" in html):
            return True, "可达"
        return False, "不可达（可能被限流或需登录）"
