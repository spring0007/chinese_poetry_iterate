# -*- coding: utf-8 -*-
"""
桌面窗口：检索 + 编辑 + 添加。

why Tkinter 而不是 Electron/Qt：零第三方依赖（标准库自带），Windows/macOS/Linux
都能跑，单文件双击即开。代价是界面朴素——对一个"查数据、录数据"的工具来说，
可读性 > 视觉效果，且省掉整条前端构建链。

三个能力（标签页 + 按钮）：
  检索：关键词 / 朝代 / 体裁 / 作者 / 词牌 过滤，结果表 + 详情（含平仄）；
        选中一条可「编辑选中」或「删除选中」——用于订正显示有误的诗词。
  添加：录入新条目。文本走与构建期完全相同的归一规则，写完立刻能被搜到。
  编辑：弹出预填窗口改 标题/作者/词牌/标签/注释/正文，保存即写回数据库；
        朝代、体裁不可改（会变 id，破坏平仄关联）。修订过的构建期数据会被
        标记为"用户修订"来源，与原始构建数据区分。

运行：
    python app.py
    python app.py --db ./dist/poetry.db --strains-db ./dist/poetry-strains.db
"""

from __future__ import annotations
import os, sys, time, threading, argparse, queue

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

import schema as S
import query as Q
import store as ST

FONT = ("Microsoft YaHei UI", 10)
FONT_SM = ("Microsoft YaHei UI", 9)
MONO = ("Consolas", 10)

HERE = os.path.dirname(os.path.abspath(__file__))


def _default(rel: str) -> str:
    return os.path.join(HERE, rel)


class App:
    def __init__(self, root: tk.Tk, db: str, strains_db: str):
        self.root = root
        self.db = db
        self.strains_db = strains_db
        root.title("诗词库 · 查询 / 编辑 / 添加")
        root.geometry("1080x720")
        root.minsize(900, 600)

        self.con = None
        self.err = ""
        # 检索分页状态：q_args 记查询条件，翻页时按同一条件取不同偏移量
        self.q_args = {}
        self.q_page = 1
        self.q_total = 0
        self.q_size = 50
        # 检索结果回传队列。why：实测（Python 3.14 / Tk 8.6）子线程里调 root.after()
        # 根本不会被执行，回调永远不触发，界面会一直停在"检索中…"。
        # 所以子线程只把结果塞进队列，由主线程用 after() 轮询取回——
        # 所有 tkinter 调用都留在主线程，这也是 Tk 唯一可靠的用法。
        self._q = queue.Queue()
        self._pumping = False
        try:
            self.con = ST.open_rw(db, strains_db)
        except Exception as e:          # 开库失败也要起来，至少能告诉用户原因
            self.err = str(e)

        # 核对状态库（独立 sidecar，重建 poetry.db 不会影响它）。rw=True 时
        # 若文件不存在会自动建表，因此首次标记核对就会生成 poetry-checks.db。
        self.chk_db = _default(os.path.join("dist", "poetry-checks.db"))
        try:
            self.chk_con = ST.open_checks(self.chk_db, rw=True)
        except Exception as e:
            self.chk_con = None
            self.err = self.err or str(e)
        self.cur_verified = False

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        self.tab_search = ttk.Frame(nb)
        self.tab_add = ttk.Frame(nb)
        nb.add(self.tab_search, text="  检索  ")
        nb.add(self.tab_add, text="  添加  ")

        self._build_search(self.tab_search)
        self._build_add(self.tab_add)
        self._build_status(root)

        if self.con is None:
            self._set_status("无法打开数据库：%s" % self.err)
        else:
            self._refresh_stats()
            self.refresh_mine()

    # ------------------------------------------------------------ 状态栏
    def _build_status(self, root):
        # side="bottom" 后 pack 的控件贴在最下；先 pack 核对栏，使它成为最底部一行。
        self.chk_status = tk.StringVar(value="")
        chkbar = ttk.Label(root, textvariable=self.chk_status, anchor="w",
                           relief="sunken", font=FONT_SM, foreground="#1a7a2a")
        chkbar.pack(fill="x", side="bottom", ipady=2)
        self.status = tk.StringVar(value="")
        bar = ttk.Label(root, textvariable=self.status, anchor="w",
                        relief="sunken", font=FONT_SM)
        bar.pack(fill="x", side="bottom", ipady=2)

    def _set_status(self, s: str):
        self.status.set(s)

    def _refresh_stats(self):
        if not self.con:
            return
        try:
            st = ST.stats(self.con)
            edited = ST.count_edited(self.con)
            strains = ""
            if self.strains_db and os.path.exists(self.strains_db) and Q.has_strains(self.con):
                n = self.con.execute("SELECT COUNT(*) FROM strainsdb.poem_strains").fetchone()[0]
                strains = "  平仄包 %d 条" % n
            else:
                strains = "  平仄包 未挂载"
            self._set_status(
                "库: %s   共 %d 条 / %d 位作者 / 我的条目 %d 条（自建 %d / 修订 %d）%s"
                % (os.path.basename(self.db), st["n_poems"], st["n_authors"],
                   ST.count_mine(self.con), st["n_user_added"], edited, strains))
            # 底部核对栏：与上方统计分离，始终显示已核对 / 待核对
            if self.chk_con:
                vc = ST.count_checked(self.chk_con)
                self.chk_status.set("核对状态：已核对 %d 篇 / 待核对 %d 篇（全库共 %d 篇）"
                                    % (vc, st["n_poems"] - vc, st["n_poems"]))
            else:
                self.chk_status.set("核对状态：核对库未打开")
        except Exception as e:
            self._set_status("统计失败：%s" % e)

    # ============================================================ 检索窗口
    def _build_search(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill="x", padx=8, pady=6)

        ttk.Label(top, text="关键词", font=FONT).grid(row=0, column=0, sticky="e", padx=(0, 4))
        self.v_kw = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.v_kw, font=FONT, width=22)
        e.grid(row=0, column=1, sticky="w")
        e.bind("<Return>", lambda _e: self.do_search())

        ttk.Label(top, text="朝代", font=FONT).grid(row=0, column=2, sticky="e", padx=(12, 4))
        self.v_dyn = tk.StringVar(value="（全部）")
        ttk.Combobox(top, textvariable=self.v_dyn, width=8, font=FONT, state="readonly",
                     values=["（全部）"] + ["%s" % S.DYNASTY_LABEL[k] for k in S.DYNASTY]
                     ).grid(row=0, column=3, sticky="w")

        ttk.Label(top, text="体裁", font=FONT).grid(row=0, column=4, sticky="e", padx=(12, 4))
        self.v_kind = tk.StringVar(value="（全部）")
        ttk.Combobox(top, textvariable=self.v_kind, width=7, font=FONT, state="readonly",
                     values=["（全部）"] + ["%s" % S.KIND_LABEL[k] for k in S.KIND]
                     ).grid(row=0, column=5, sticky="w")

        ttk.Label(top, text="作者", font=FONT).grid(row=1, column=0, sticky="e", padx=(0, 4), pady=(6, 0))
        self.v_author = tk.StringVar()
        ttk.Entry(top, textvariable=self.v_author, font=FONT, width=12).grid(
            row=1, column=1, sticky="w", pady=(6, 0))

        ttk.Label(top, text="词牌", font=FONT).grid(row=1, column=2, sticky="e", padx=(12, 4), pady=(6, 0))
        self.v_rhy = tk.StringVar()
        ttk.Entry(top, textvariable=self.v_rhy, font=FONT, width=12).grid(
            row=1, column=3, sticky="w", pady=(6, 0))

        ttk.Label(top, text="每页", font=FONT).grid(row=1, column=4, sticky="e", padx=(12, 4), pady=(6, 0))
        self.v_limit = tk.IntVar(value=50)
        ttk.Spinbox(top, from_=10, to=500, increment=10, textvariable=self.v_limit,
                    width=6, font=FONT).grid(row=1, column=5, sticky="w", pady=(6, 0))

        self.btn_search = ttk.Button(top, text="检索", command=self.do_search)
        self.btn_search.grid(row=0, column=6, rowspan=2, padx=(16, 0))
        ttk.Button(top, text="清空", command=self._clear_search).grid(row=0, column=7, rowspan=2, padx=(6, 0))

        # 结果表
        mid = ttk.Frame(parent)
        mid.pack(fill="both", expand=True, padx=8)
        cols = ("id", "dyn", "kind", "author", "title", "preview", "chk")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=12)
        heads = {"id": ("id", 90), "dyn": ("朝代", 60), "kind": ("体裁", 50),
                 "author": ("作者", 120), "title": ("标题", 180),
                 "preview": ("正文", 360), "chk": ("核对", 44)}
        for c in cols:
            t, w = heads[c]
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w" if c != "id" else "center")
        vs = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.show_detail())
        self._build_context_menu()

        # 分页栏：第 X / Y 页（共 N 条）+ 首页/上/下/末 + 跳页
        pg = ttk.Frame(parent)
        pg.pack(fill="x", padx=8, pady=(2, 4))
        self.btn_first = ttk.Button(pg, text="首页", width=5, command=lambda: self.goto_page(1))
        self.btn_prev = ttk.Button(pg, text="上一页", width=6, command=lambda: self.goto_page(self.q_page - 1))
        self.pg_label = ttk.Label(pg, text="第 1 / 1 页（共 0 条）", font=FONT_SM, width=24, anchor="center")
        self.btn_next = ttk.Button(pg, text="下一页", width=6, command=lambda: self.goto_page(self.q_page + 1))
        self.btn_last = ttk.Button(pg, text="末页", width=5, command=lambda: self.goto_page(10 ** 9))
        self.pg_jump = tk.IntVar(value=1)
        ent_jump = ttk.Entry(pg, textvariable=self.pg_jump, width=5, font=FONT_SM)
        btn_jump = ttk.Button(pg, text="跳转", width=5, command=lambda: self.goto_page(self.pg_jump.get()))
        for w in (self.btn_first, self.btn_prev, self.pg_label, self.btn_next,
                  self.btn_last, ttk.Label(pg, text="跳到", font=FONT_SM),
                  ent_jump, btn_jump):
            w.pack(side="left", padx=2)
        self._render_pager()

        # 详情
        ttk.Label(parent, text="详情", font=FONT).pack(anchor="w", padx=8, pady=(6, 0))
        self.detail = scrolledtext.ScrolledText(parent, height=10, font=MONO, wrap="word")
        self.detail.pack(fill="both", expand=False, padx=8, pady=(0, 8))
        self.detail.configure(state="disabled")

        ebf = ttk.Frame(parent)
        ebf.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(ebf, text="编辑诗词", command=self.edit_selected).pack(side="left")
        ttk.Button(ebf, text="编辑作者", command=self.edit_author_of_selected).pack(side="left", padx=6)
        ttk.Button(ebf, text="删除选中", command=self.del_selected).pack(side="left", padx=6)
        self.btn_chk = ttk.Button(ebf, text="标记已核对", command=self.toggle_checked)
        self.btn_chk.pack(side="left", padx=6)
        ttk.Label(ebf, text="选中一条可订正文/作者，或删除（右键也有菜单）",
                  font=FONT_SM, foreground="#888").pack(side="left", padx=12)

    def _clear_search(self):
        for v in (self.v_kw, self.v_author, self.v_rhy):
            v.set("")
        self.v_dyn.set("（全部）")
        self.v_kind.set("（全部）")
        for i in self.tree.get_children():
            self.tree.delete(i)
        self._detail_text("")

    def _dyn_key(self) -> str:
        lab = self.v_dyn.get()
        for k, v in S.DYNASTY_LABEL.items():
            if v == lab:
                return k
        return ""

    def _kind_key(self) -> str:
        lab = self.v_kind.get()
        for k, v in S.KIND_LABEL.items():
            if v == lab:
                return k
        return ""

    def do_search(self, page: int = 1):
        if not self.con:
            messagebox.showerror("错误", "数据库未打开：%s" % self.err)
            return
        kw = self.v_kw.get().strip()
        author = self.v_author.get().strip()
        rhy = self.v_rhy.get().strip()
        if not kw and not author and not rhy:
            messagebox.showinfo("提示", "请至少填 关键词 / 作者 / 词牌 之一")
            return
        # 记下本次查询条件，翻页时按同一条件重新取偏移量
        self.q_args = dict(kw=kw, dynasty=self._dyn_key(), kind=self._kind_key(),
                           author=author, rhy=rhy)
        self.q_size = max(1, int(self.v_limit.get()))
        self.q_page = page
        self.btn_search.configure(state="disabled", text="检索中…")
        self._set_status("检索中…")
        q, size, off = self.q_args, self.q_size, (page - 1) * self.q_size

        def worker():
            # 子线程：里面不能出现任何 tkinter 调用（实测 Tk 8.6 跨线程 after() 无效）。
            t0 = time.time()
            rows, total, err = [], 0, ""
            try:
                con = ST.open_ro(self.db, self.strains_db)   # 子线程必须用自己的连接
                try:
                    total, rows = Q.run_query(con, **q, limit=size, offset=off)
                finally:
                    con.close()
            except Exception as e:
                err = "%s: %s" % (type(e).__name__, e)
            dt = (time.time() - t0) * 1000
            self._q.put((total, rows, dt, err))    # 不碰 tkinter，只交结果

        threading.Thread(target=worker, daemon=True).start()
        # 主线程起轮询（after 只能在主线程调，且由主线程自己重排）
        if not self._pumping:
            self._pumping = True
            self.root.after(30, self._pump)

    def _pump(self):
        """主线程轮询取回检索结果。没结果就 30 ms 后再来一次。"""
        item = None
        while True:                      # 取到最后一条为止：用户连点检索时，
            try:                         # 中间的旧结果直接丢掉，只渲染最新的
                item = self._q.get_nowait()
            except queue.Empty:
                break
        if item is None:
            self.root.after(30, self._pump)
            return
        self._pumping = False
        self._on_searched(*item)

    def _on_searched(self, total, rows, dt, err):
        self.btn_search.configure(state="normal", text="检索")
        for i in self.tree.get_children():
            self.tree.delete(i)
        if err:
            self._set_status("查询失败：%s" % err)
            self.q_total = 0
            self._render_pager()
            return
        for r in rows:
            prev = r["body"].replace("\n", "／")
            if len(prev) > 60:
                prev = prev[:60] + "…"
            self.tree.insert("", "end", iid=str(r["id"]), values=(
                r["id"], S.DYNASTY_LABEL.get(r["dynasty"], r["dynasty"]),
                S.KIND_LABEL.get(r["kind"], r["kind"]),
                r["author"] or "佚名", r["title"], prev, ""))
        # 批量取本页核对状态，打勾
        if self.chk_con and rows:
            vmap = ST.checked_batch(self.chk_con, [r["id"] for r in rows])
            for r in rows:
                if vmap.get(r["id"]):
                    self.tree.set(str(r["id"]), "chk", "✓")
        self.q_total = total
        self._render_pager()
        self._set_status("命中 %d 条，本页 %d 条，耗时 %.0f ms" % (total, len(rows), dt))
        if rows:
            self.tree.selection_set(str(rows[0]["id"]))
            self.show_detail()

    def _render_pager(self):
        """根据 q_total / q_page / q_size 刷新分页标签与按钮可用性。"""
        size = self.q_size
        total = self.q_total
        pages = max(1, (total + size - 1) // size) if total else 1
        self.pg_label.configure(
            text="第 %d / %d 页（共 %d 条）" % (self.q_page, pages, total))
        dis = "disabled"
        self.btn_prev.configure(state=dis if self.q_page <= 1 else "normal")
        self.btn_first.configure(state=dis if self.q_page <= 1 else "normal")
        self.btn_next.configure(state=dis if self.q_page >= pages else "normal")
        self.btn_last.configure(state=dis if self.q_page >= pages else "normal")

    def goto_page(self, n: int):
        """翻到第 n 页（沿用上次查询条件）。n 越界会被夹到 [1, pages]。"""
        if not self.q_args:
            return
        self.pg_jump.set(max(1, n))
        self.do_search(max(1, n))

    def show_detail(self):
        sel = self.tree.selection()
        if not sel or not self.con:
            return
        pid = int(sel[0])
        self.cur_pid = pid
        r = Q.get_by_id(self.con, pid)
        if not r:
            self._detail_text("未找到 id=%d" % pid)
            return
        self.cur_aid = r.get("author_id", 0)
        # 当前诗词的核对状态（独立 sidecar，不影响检索与构建）
        self.cur_verified = bool(self.chk_con and ST.get_checked(self.chk_con, pid))
        self.btn_chk.configure(text="取消核对" if self.cur_verified else "标记已核对")
        lines = []
        lines.append("id=%d   [%s/%s]   score=%d  字数=%d  来源=%s"
                     % (r["id"], S.DYNASTY_LABEL.get(r["dynasty"], r["dynasty"]),
                        S.KIND_LABEL.get(r["kind"], r["kind"]),
                        r["score"], r["n_char"], r["src"]))
        adyn = S.DYNASTY_LABEL.get(r.get("author_dynasty") or "", r.get("author_dynasty") or "")
        lines.append("《%s》  %s%s%s" % (
            r["title"], r["author"] or "佚名",
            (" ［%s］" % adyn) if adyn else "",
            ("  [%s]" % r["rhythmic"]) if r["rhythmic"] else ""))
        if r.get("author_desc"):
            lines.append("作者小传：" + r["author_desc"][:300])
        lines.append("")
        body_lines = r["body"].split("\n")
        st = r.get("strains_text") or ""
        if st:
            # 平仄与正文逐字对应，按行切分时用各行字数对齐
            pos = 0
            for bl in body_lines:
                seg = st[pos:pos + len(bl)]
                pos += len(bl)
                lines.append(bl)
                lines.append(seg)
        else:
            lines.extend(body_lines)
        if r["tags"]:
            lines.append("")
            lines.append("标签：" + ", ".join(r["tags"].split("\x1f")))
        if r["notes"]:
            lines.append("注释：" + ", ".join(r["notes"].split("\x1f"))[:400])
        lines.append("")
        lines.append("核对状态：%s" % ("已核对 ✓" if self.cur_verified else "未核对"))
        self._detail_text("\n".join(lines))

    def _detail_text(self, s: str):
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", s)
        self.detail.configure(state="disabled")

    # ------------------------------------------------------------ 编辑 / 删除
    def edit_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先在上方结果里选中一条，再点「编辑选中」")
            return
        self.open_editor(int(sel[0]))

    def edit_mine(self):
        sel = self.mine.selection()
        if not sel:
            messagebox.showinfo("提示", "先在下方「我的条目」里选中一条，再点「编辑选中」")
            return
        self.open_editor(int(sel[0]))

    def del_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先在上方结果里选中一条，再点「删除选中」")
            return
        self.delete_by_pid(int(sel[0]))

    def delete_by_pid(self, pid: int, force: bool = False):
        if not messagebox.askyesno("确认删除", "确定删除 id=%d 吗？此操作不可撤销。" % pid):
            return
        try:
            ST.delete_poem(self.con, pid, force=force)
        except ST.Rejected as e:
            # 构建期原始数据默认删不掉——这里再给一次"强制删除"的机会。
            if force or not messagebox.askyesno(
                    "注意", "%s\n仍要强制删除这条原始数据吗？\n（删除后只能重建数据库恢复）" % e):
                messagebox.showwarning("未删除", str(e))
                return
            try:
                ST.delete_poem(self.con, pid, force=True)
            except ST.Rejected as e2:
                messagebox.showwarning("未删除", str(e2))
                return
        if str(pid) in self.tree.get_children():
            self.tree.delete(str(pid))
        self.refresh_mine()
        self._refresh_stats()
        self._set_status("已删除 id=%d" % pid)

    def toggle_checked(self):
        """标记 / 取消「已核对」。写入独立 sidecar 库，不受重建影响。"""
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在上方结果里选中一条诗词，再标记核对状态")
            return
        pid = int(sel[0])
        if not self.chk_con:
            try:
                self.chk_con = ST.open_checks(self.chk_db, rw=True)
            except Exception as e:
                messagebox.showerror("错误", "核对库打开失败：%s" % e)
                return
        cur = ST.get_checked(self.chk_con, pid)
        ST.set_checked(self.chk_con, pid, not cur)
        self.tree.set(str(pid), "chk", "✓" if not cur else "")
        self.show_detail()
        self._refresh_stats()
        self._set_status("已%s id=%d" % ("标记为已核对" if not cur else "取消核对", pid))

    # ------------------------------------------------------------ 作者档案编辑
    def edit_author_of_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先在上方结果里选中一条，再点「编辑作者」")
            return
        pid = int(sel[0])
        row = self.con.execute("SELECT author_id FROM poems WHERE id=?", (pid,)).fetchone()
        if not row or not row[0]:
            messagebox.showinfo("提示", "该诗作者为佚名（无作者记录），暂不支持编辑作者简介")
            return
        self.open_author_editor(row[0])

    def open_author_editor(self, aid: int):
        """编辑作者小传与朝代（不改诗词本身的朝代/体裁，不影响检索与平仄关联）。"""
        if not self.con:
            messagebox.showerror("错误", "数据库未打开：%s" % self.err)
            return
        row = self.con.execute(
            "SELECT id,name,dynasty,desc FROM authors WHERE id=?", (aid,)).fetchone()
        if row is None:
            messagebox.showerror("错误", "未找到作者 id=%d" % aid)
            return

        win = tk.Toplevel(self.root)
        win.title("编辑作者  %s" % row["name"])
        win.geometry("480x460")
        win.minsize(380, 360)

        v_name = tk.StringVar(value=row["name"])
        dkey = row["dynasty"] if row["dynasty"] in S.DYNASTY else "custom"
        v_dyn = tk.StringVar(value=S.DYNASTY_LABEL[dkey])

        f = ttk.Frame(win)
        f.pack(fill="x", padx=10, pady=8)
        ttk.Label(f, text="作者名", font=FONT).grid(row=0, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Entry(f, textvariable=v_name, font=FONT, width=26).grid(row=0, column=1, sticky="w", pady=3)
        ttk.Label(f, text="朝代", font=FONT).grid(row=1, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Combobox(f, textvariable=v_dyn, width=8, font=FONT, state="readonly",
                     values=[S.DYNASTY_LABEL[k] for k in S.DYNASTY]
                     ).grid(row=1, column=1, sticky="w", pady=3)

        ttk.Label(win, text="简介 / 作者小传", font=FONT).pack(anchor="w", padx=10, pady=(4, 0))
        desc = scrolledtext.ScrolledText(win, height=16, font=FONT, wrap="word")
        desc.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        desc.insert("1.0", row["desc"] or "")

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=10, pady=6)

        def _dyn_label_to_key(lab):
            for k, v in S.DYNASTY_LABEL.items():
                if v == lab:
                    return k
            return "custom"

        def do_save():
            try:
                ST.update_author(self.con, aid, name=v_name.get(),
                                 dynasty=_dyn_label_to_key(v_dyn.get()),
                                 desc=desc.get("1.0", "end").strip())
            except ST.Rejected as e:
                messagebox.showwarning("未保存", str(e))
                return
            messagebox.showinfo("已保存", "作者《%s》信息已更新" % v_name.get())
            win.destroy()
            self.show_detail()
            self._refresh_stats()

        ttk.Button(btns, text="保存", command=do_save).pack(side="left")
        ttk.Button(btns, text="取消", command=win.destroy).pack(side="left", padx=8)
        ttk.Label(btns, text="改的是作者档案，不会动到诗词本身的朝代/体裁",
                  font=FONT_SM, foreground="#888").pack(side="left", padx=10)
        win.transient(self.root)
        win.grab_set()
        win.wait_visibility()
        desc.focus_set()

    # ------------------------------------------------------------ 右键菜单
    def _build_context_menu(self):
        self.ctx = tk.Menu(self.root, tearoff=0)
        self.ctx.add_command(label="编辑诗词", command=self.edit_selected)
        self.ctx.add_command(label="编辑作者", command=self.edit_author_of_selected)
        self.ctx.add_command(label="标记已核对/取消", command=self.toggle_checked)
        self.ctx.add_separator()
        self.ctx.add_command(label="删除", command=self.del_selected)
        self.tree.bind("<Button-3>", self._on_tree_rightclick)

    def _on_tree_rightclick(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        self.show_detail()
        try:
            self.ctx.tk_popup(event.x_root, event.y_root)
        finally:
            self.ctx.grab_release()

    def open_editor(self, pid: int):
        """弹出修订窗口，预填当前记录；保存时走 store.update_poem 写回数据库。"""
        if not self.con:
            messagebox.showerror("错误", "数据库未打开：%s" % self.err)
            return
        rec = Q.get_by_id(self.con, pid)
        if not rec:
            messagebox.showerror("错误", "未找到 id=%d" % pid)
            return

        win = tk.Toplevel(self.root)
        win.title("编辑诗词  id=%d" % pid)
        win.geometry("600x680")
        win.minsize(460, 520)

        v_title = tk.StringVar(value=rec["title"])
        v_author = tk.StringVar(value=rec["author"] or "")
        v_rhy = tk.StringVar(value=rec["rhythmic"] or "")
        v_tags = tk.StringVar(value=", ".join(rec["tags"].split("\x1f")) if rec["tags"] else "")
        v_notes = tk.StringVar(value=", ".join(rec["notes"].split("\x1f")) if rec["notes"] else "")
        dyn_lab = S.DYNASTY_LABEL.get(rec["dynasty"], rec["dynasty"])
        kind_lab = S.KIND_LABEL.get(rec["kind"], rec["kind"])

        f = ttk.Frame(win)
        f.pack(fill="x", padx=10, pady=8)
        field_rows = [("标题", v_title), ("作者", v_author), ("词牌/曲牌", v_rhy),
                      ("标签(逗号分隔)", v_tags), ("注释(逗号分隔)", v_notes)]
        for i, (lab, var) in enumerate(field_rows):
            ttk.Label(f, text=lab, font=FONT).grid(row=i, column=0, sticky="e", padx=(0, 6), pady=3)
            ttk.Entry(f, textvariable=var, font=FONT, width=42).grid(row=i, column=1, sticky="w", pady=3)
        ttk.Label(f, text="朝代", font=FONT).grid(row=5, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Label(f, text=dyn_lab + "（不可改，改动需重建）", font=FONT_SM, foreground="#666").grid(
            row=5, column=1, sticky="w", pady=3)
        ttk.Label(f, text="体裁", font=FONT).grid(row=6, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Label(f, text=kind_lab + "（不可改，保留 id 以维持平仄关联）", font=FONT_SM, foreground="#666").grid(
            row=6, column=1, sticky="w", pady=3)

        ttk.Label(win, text="正文（一行一句，空行忽略）", font=FONT).pack(anchor="w", padx=10, pady=(4, 0))
        body = scrolledtext.ScrolledText(win, height=16, font=FONT, wrap="word")
        body.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        body.insert("1.0", rec["body"])

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=10, pady=6)

        def do_save():
            raw = body.get("1.0", "end")
            lines = [x.strip() for x in raw.splitlines() if x.strip()]
            tags = [x.strip() for x in v_tags.get().split(",") if x.strip()]
            notes = [x.strip() for x in v_notes.get().split(",") if x.strip()]
            try:
                ST.update_poem(self.con, pid, title=v_title.get(), author=v_author.get(),
                               lines=lines, rhythmic=v_rhy.get(), tags=tags, notes=notes)
            except ST.Rejected as e:
                messagebox.showwarning("未保存", str(e))
                return
            except Exception as e:            # 落库异常不能让窗口卡死
                messagebox.showerror("写入失败", "%s: %s" % (type(e).__name__, e))
                return
            messagebox.showinfo("已保存", "id=%d 《%s》已修订并写回数据库" % (pid, v_title.get()))
            win.destroy()
            if str(pid) in self.tree.get_children():
                self.tree.selection_set(str(pid))
                self.show_detail()
            self.refresh_mine()
            self._refresh_stats()

        ttk.Button(btns, text="保存", command=do_save).pack(side="left")
        ttk.Button(btns, text="取消", command=win.destroy).pack(side="left", padx=8)
        ttk.Label(btns, text='修改会标记为"用户修订"，可重新检索', font=FONT_SM,
                  foreground="#888").pack(side="left", padx=10)
        win.transient(self.root)
        win.grab_set()
        win.wait_visibility()
        body.focus_set()

    # ============================================================ 添加窗口
    def _build_add(self, parent):
        form = ttk.Frame(parent)
        form.pack(fill="x", padx=8, pady=6)

        self.v_t_title = tk.StringVar()
        self.v_t_author = tk.StringVar()
        self.v_t_rhy = tk.StringVar()
        self.v_t_tags = tk.StringVar()
        self.v_t_dyn = tk.StringVar(value=S.DYNASTY_LABEL["custom"])
        self.v_t_kind = tk.StringVar(value=S.KIND_LABEL["poem"])

        rows = [
            ("标题", self.v_t_title, 0, 0, 30),
            ("作者", self.v_t_author, 0, 2, 16),
            ("朝代", None, 0, 4, 10),
            ("体裁", None, 1, 4, 10),
            ("词牌/曲牌", self.v_t_rhy, 1, 0, 30),
            ("标签(逗号分隔)", self.v_t_tags, 1, 2, 16),
        ]
        for lab, var, r, c, w in rows:
            ttk.Label(form, text=lab, font=FONT).grid(row=r, column=c, sticky="e", padx=(0, 4), pady=3)
            if var is None:
                continue
            ttk.Entry(form, textvariable=var, font=FONT, width=w).grid(row=r, column=c + 1, sticky="w", pady=3)
        ttk.Combobox(form, textvariable=self.v_t_dyn, width=8, font=FONT, state="readonly",
                     values=[S.DYNASTY_LABEL[k] for k in S.DYNASTY]
                     ).grid(row=0, column=5, sticky="w", pady=3)
        ttk.Combobox(form, textvariable=self.v_t_kind, width=8, font=FONT, state="readonly",
                     values=[S.KIND_LABEL[k] for k in S.KIND]
                     ).grid(row=1, column=5, sticky="w", pady=3)

        ttk.Label(parent, text="正文（一行一句，空行忽略）", font=FONT).pack(anchor="w", padx=8)
        self.body_text = scrolledtext.ScrolledText(parent, height=8, font=FONT, wrap="word")
        self.body_text.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        btns = ttk.Frame(parent)
        btns.pack(fill="x", padx=8, pady=4)
        ttk.Button(btns, text="提交", command=self.do_add).pack(side="left")
        ttk.Button(btns, text="清空表单", command=self._clear_form).pack(side="left", padx=6)
        ttk.Label(btns, text="提交后立即可被检索到；繁体会自动转简体、空格自动去除",
                  font=FONT_SM, foreground="#888").pack(side="left", padx=12)

        ttk.Label(parent, text="我的条目（自建 / 修订，可编辑或删除）", font=FONT).pack(anchor="w", padx=8, pady=(8, 0))
        box = ttk.Frame(parent)
        box.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.mine = ttk.Treeview(box, columns=("id", "title", "author", "n", "src"),
                                 show="headings", height=6)
        for c, (t, w) in {"id": ("id", 100), "title": ("标题", 230),
                          "author": ("作者", 130), "n": ("字数", 60),
                          "src": ("来源", 90)}.items():
            self.mine.heading(c, text=t)
            self.mine.column(c, width=w, anchor="w")
        vsb = ttk.Scrollbar(box, orient="vertical", command=self.mine.yview)
        self.mine.configure(yscrollcommand=vsb.set)
        self.mine.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        bb = ttk.Frame(parent)
        bb.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(bb, text="刷新", command=self.refresh_mine).pack(side="left")
        ttk.Button(bb, text="编辑选中", command=self.edit_mine).pack(side="left", padx=6)
        ttk.Button(bb, text="删除选中", command=self.do_delete).pack(side="left", padx=6)

    def _clear_form(self):
        for v in (self.v_t_title, self.v_t_author, self.v_t_rhy, self.v_t_tags):
            v.set("")
        self.body_text.delete("1.0", "end")

    def _t_dyn_key(self) -> str:
        lab = self.v_t_dyn.get()
        for k, v in S.DYNASTY_LABEL.items():
            if v == lab:
                return k
        return "custom"

    def _t_kind_key(self) -> str:
        lab = self.v_t_kind.get()
        for k, v in S.KIND_LABEL.items():
            if v == lab:
                return k
        return "poem"

    def do_add(self):
        if not self.con:
            messagebox.showerror("错误", "数据库未打开：%s" % self.err)
            return
        raw = self.body_text.get("1.0", "end")
        lines = [x.strip() for x in raw.splitlines()]
        lines = [x for x in lines if x]
        tags = [x.strip() for x in self.v_t_tags.get().split(",") if x.strip()]
        try:
            r = ST.add_poem(self.con, dynasty=self._t_dyn_key(), kind=self._t_kind_key(),
                            title=self.v_t_title.get(), author=self.v_t_author.get(),
                            lines=lines, rhythmic=self.v_t_rhy.get(), tags=tags)
        except ST.Rejected as e:
            messagebox.showwarning("未提交", str(e))
            return
        except Exception as e:
            messagebox.showerror("写入失败", "%s: %s" % (type(e).__name__, e))
            return
        self._clear_form()
        self.refresh_mine()
        self._refresh_stats()
        self._set_status("已写入 id=%d 《%s》（%d 字）" % (r["id"], r["title"], r["n_char"]))

    def refresh_mine(self):
        if not self.con:
            return
        for i in self.mine.get_children():
            self.mine.delete(i)
        for r in ST.list_mine(self.con, 500):
            src_lab = "修订" if r["src"] == ST.EDIT_SRC else "自建"
            self.mine.insert("", "end", iid=str(r["id"]),
                             values=(r["id"], r["title"], r["author"] or "佚名",
                                     r["n_char"], src_lab))

    def do_delete(self):
        sel = self.mine.selection()
        if not sel:
            messagebox.showinfo("提示", "先在下方「我的条目」里选中一条")
            return
        self.delete_by_pid(int(sel[0]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=_default(os.path.join("dist", "poetry.db")))
    ap.add_argument("--strains-db", default=_default(os.path.join("dist", "poetry-strains.db")))
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit("找不到数据库：%s\n先跑：python build.py --out ./dist" % args.db)

    root = tk.Tk()
    style = ttk.Style()
    if "vista" in style.theme_names():
        style.theme_use("vista")
    App(root, args.db, args.strains_db)
    root.mainloop()


if __name__ == "__main__":
    main()
