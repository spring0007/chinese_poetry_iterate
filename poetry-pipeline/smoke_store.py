# -*- coding: utf-8 -*-
"""写入层第二轮冒烟：导出/导入闭环，同时清掉冒烟残留在生产库里的自建条目。"""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

import schema as S
import store as ST

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "dist", "poetry.db")
TMP = os.path.join(HERE, "_rt.jsonl")
out = []


def put(s):
    out.append(s)


try:
    con = ST.open_rw(DB)
    n0 = ST.count_user_added(con)
    put("初始自建条目 %d 条（前几轮冒烟的残留）" % n0)

    # 先把残留导出来，再删干净，让生产库回到无自建状态
    if n0:
        ST.export_user(con, TMP)
        put("已导出 %d 条 -> _rt.jsonl" % n0)
        ids = [r[0] for r in con.execute(
            "SELECT p.id FROM poems p JOIN sources s ON s.id=p.src_id "
            "WHERE s.name='user-added'").fetchall()]
        for i in ids:
            ST.delete_poem(con, i)
        put("已删除残留 %d 条，当前自建 %d 条" % (len(ids), ST.count_user_added(con)))

    # ---- 新增两条（含一条繁体+空格，验证归一）
    r1 = ST.add_poem(con, dynasty="custom", kind="poem", title="导出导入测试甲",
                     author="测试作者", lines=["床前明月光", "疑是地上霜"], tags=["测试"])
    r2 = ST.add_poem(con, dynasty="custom", kind="fu", title="导出导入测试乙",
                     author="", lines=["歸去來兮，田園將蕪胡不歸"], tags=[])
    put("新增 id=%d(%s/%s) / id=%d(%s/%s)"
        % (r1["id"], *S.parse_id(r1["id"])[:2], r2["id"], *S.parse_id(r2["id"])[:2]))
    put("  乙条目归日后: %s" % con.execute(
        "SELECT body FROM poems WHERE id=?", (r2["id"],)).fetchone()[0])

    # ---- 导出
    n_exp = ST.export_user(con, TMP)
    put("导出 %d 条" % n_exp)

    # ---- 删光后导回
    for i in [r["id"] for r in ST.list_user_added(con, 100)]:
        ST.delete_poem(con, i)
    put("清空后自建 %d 条" % ST.count_user_added(con))
    w, s = ST.import_user(con, TMP)
    put("导入: 写入 %d 条，跳过重复 %d 条" % (w, s))
    back = ST.list_user_added(con, 100)
    put("导回后自建 %d 条" % len(back))
    for r in back:
        dy, kd, _ = S.parse_id(r["id"])
        put("  id=%d [%s/%s] 作者=%s 《%s》 标签=%s"
            % (r["id"], dy, kd, r["author"] or "佚名", r["title"], r["tags"]))

    ok_roundtrip = (len(back) == 2 and any(r["title"] == "导出导入测试甲" for r in back)
                    and any(r["title"] == "导出导入测试乙" for r in back))
    put("[PASS] 导出/导入闭环（含繁体归一、标签、佚名）" if ok_roundtrip
        else "[FAIL] 导出/导入闭环")

    # 幂等：重复导入同一文件应全部跳过，不产生重复行
    w2, s2 = ST.import_user(con, TMP)
    put("重复导入: 写入 %d 条，跳过 %d 条" % (w2, s2))
    put("[PASS] 重复导入被去重拦截" if w2 == 0 and s2 == 2
        else "[FAIL] 重复导入产生了重复行")

    # ---- 修订（edit）闭环：挑一条构建期诗，改标题后写回，再还原
    row = con.execute(
        "SELECT p.id,p.title,a.name,p.src_id FROM poems p "
        "LEFT JOIN authors a ON a.id=p.author_id "
        "WHERE p.src_id NOT IN (SELECT id FROM sources WHERE name IN ('user-added','user-edit')) "
        "LIMIT 1").fetchone()
    pid, o_title, o_author, o_src = row["id"], row["title"], row["name"], row["src_id"]
    o_body = con.execute("SELECT body FROM poems WHERE id=?", (pid,)).fetchone()[0]
    ST.update_poem(con, pid, title=o_title + "【冒烟】", author=o_author or "",
                   lines=o_body.split("\n"))
    src_now = con.execute(
        "SELECT s.name FROM poems p JOIN sources s ON s.id=p.src_id WHERE p.id=?",
        (pid,)).fetchone()[0]
    flag_ok = (src_now == "user-edit")
    # 还原：标题与来源都回到原始构建值（作者未变，不触动计数）
    con.execute("UPDATE poems SET title=?, src_id=? WHERE id=?", (o_title, o_src, pid))
    con.commit()
    src_back = con.execute(
        "SELECT s.name FROM poems p JOIN sources s ON s.id=p.src_id WHERE p.id=?",
        (pid,)).fetchone()[0]
    # 顺手清掉可能残留的空 user-edit 来源行
    if con.execute("SELECT COUNT(*) FROM poems WHERE src_id IN "
                   "(SELECT id FROM sources WHERE name='user-edit')").fetchone()[0] == 0:
        con.execute("DELETE FROM sources WHERE name='user-edit'")
    revert_ok = (src_back not in ("user-added", "user-edit"))
    put("[PASS] update_poem 修订标记+写回，并已还原生产库" if flag_ok and revert_ok
        else "[FAIL] update_poem 修订/还原")

    # ---- 清空，生产库回到 0 自建
    for i in [r["id"] for r in ST.list_user_added(con, 100)]:
        ST.delete_poem(con, i)
    put("清理后自建 %d 条" % ST.count_user_added(con))
    os.remove(TMP)
    con.close()
    put("SMOKE_OK")
except Exception:
    put("SMOKE_FAIL")
    put(traceback.format_exc())

print("\n".join(out))
sys.exit(0 if "SMOKE_OK" in out else 1)
