# -*- coding: utf-8 -*-
"""GUI 冒烟：真正构造一遍窗口再销毁，验证控件代码可执行（不进 mainloop）。"""
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

out = []
try:
    import tkinter as tk
    out.append("tkinter OK  TkVersion=%s" % tk.TkVersion)
    import app as A
    import query as Q
    import store as ST
    out.append("import app / query / store OK")

    here = os.path.dirname(os.path.abspath(__file__))
    db = os.path.join(here, "dist", "poetry.db")
    sdb = os.path.join(here, "dist", "poetry-strains.db")

    root = tk.Tk()
    root.withdraw()                      # 不真的把窗口显示出来
    w = A.App(root, db, sdb if os.path.exists(sdb) else None)
    root.update_idletasks()
    nb = w.tab_search.master             # Notebook
    ntab = nb.index("end")
    out.append("App 构造 OK  标签页数=%d" % ntab)
    out.append("标签页: %s" % " / ".join(nb.tab(i, "text").strip() for i in range(ntab)))

    # ---- 检索：直接调查询层填表，再走 show_detail（含平仄渲染）
    rows = Q.search(w.con, "明月", limit=5)
    out.append("检索『明月』 返回 %d 条" % len(rows))
    for r in rows:
        prev = r["body"].replace("\n", "／")[:40]
        w.tree.insert("", "end", iid=str(r["id"]),
                      values=(r["id"], r["dynasty"], r["kind"],
                              r["author"] or "佚名", r["title"], prev))
    w.tree.selection_set(str(rows[0]["id"]))
    w.show_detail()
    txt = w.detail.get("1.0", "end")
    out.append("详情面板 %d 字符，含平仄行=%s" % (len(txt), "平" in txt or "仄" in txt))

    # ---- 标题兜底：关键词不在正文里（如《静夜思》）也要能搜到
    for i in w.tree.get_children():      # 先清掉上一轮的行，否则会数到残留
        w.tree.delete(i)
    w.v_kw.set("静夜思")
    w.v_dyn.set("（全部）")
    w.v_kind.set("（全部）")
    w.do_search()
    # 检索在子线程：do_search 会立刻把按钮置 disabled，用它判断"有没有真的在跑"
    assert str(w.btn_search["state"]) == "disabled", "按钮未进入检索态，测不出异步结果"
    for _ in range(2000):                # 用 update 把 after() 回调抽干
        root.update()
        if str(w.btn_search["state"]) == "normal":
            break
        time.sleep(0.005)
    n2 = len(w.tree.get_children())
    out.append("关键词『静夜思』（仅在标题中）命中 %d 条，状态栏：%s"
               % (n2, w.status.get()))
    out.append("[PASS] 标题兜底检索" if n2 > 0 else "[FAIL] 标题兜底检索")

    # ---- 检索窗口的过滤条件反解
    w.v_dyn.set("唐")
    w.v_kind.set("诗")
    out.append("朝代/体裁下拉反解: %s / %s" % (w._dyn_key(), w._kind_key()))

    # ---- 添加窗口：录入繁体+空格，验证落库时已归一
    before = ST.count_user_added(w.con)
    w.v_t_title.set("冒烟条目静夜思")
    w.v_t_author.set("冒烟作者")
    w.v_t_dyn.set("唐")
    w.v_t_kind.set("诗")
    w.body_text.insert("1.0", "牀前明月光\n疑是地上霜\n举头望明月\n低头思故乡")
    w.do_add()
    after = ST.count_user_added(w.con)
    out.append("新增: user-added %d -> %d" % (before, after))
    row = w.con.execute(
        "SELECT p.id, p.title, p.body FROM poems p JOIN sources s ON s.id=p.src_id "
        "WHERE s.name='user-added' ORDER BY p.id DESC LIMIT 1").fetchone()
    if row is None:
        out.append("[FAIL] 提交后没有落库")
    else:
        dy, kd, _ = __import__("schema").parse_id(row[0])
        out.append("落库: id=%d 朝代=%s/%s 《%s》 %s"
                   % (row[0], dy, kd, row[1], row[2].replace("\n", "／")))
        bad = []
        if "牀" in row[2] or "牀" in row[1]:
            bad.append("繁体未转简体")
        if " " in row[2] or "　" in row[2]:
            bad.append("空格未去除")
        out.append(("[FAIL] " + "、".join(bad)) if bad else
                   "[PASS] 新增条目已简体归一、无空格（『牀』→『床』）")

    # ---- 新增条目立刻可被检索（正文命中走 search，标题命中走 by_title）
    hit = Q.by_title(w.con, "冒烟条目", limit=5)
    out.append("新增后按标题可检索到: %d 条" % len(hit))

    # ---- 删除：只能删自建，构建期数据删不掉
    try:
        ST.delete_poem(w.con, row[0])
        out.append("[PASS] 自建条目可删除")
    except ST.Rejected as e:
        out.append("[FAIL] 自建条目删不掉: %s" % e)
    built = w.con.execute(
        "SELECT id FROM poems WHERE (id>>27)<>15 LIMIT 1").fetchone()[0]
    try:
        ST.delete_poem(w.con, built)
        out.append("[FAIL] 构建期数据被删掉了（不该发生）")
        w.con.execute("ROLLBACK")
    except ST.Rejected as e:
        out.append("[PASS] 构建期数据拒绝删除: %s" % e)

    left = ST.count_user_added(w.con)
    out.append("清理后自建条目剩余 %d 条" % left)
    if left != before:
        out.append("[WARN] custom 号段未回到初始状态 %d" % before)

    root.destroy()
    out.append("SMOKE_OK")
except Exception:
    out.append("SMOKE_FAIL")
    out.append(traceback.format_exc())

print("\n".join(out))
sys.exit(0 if "SMOKE_OK" in out else 1)
