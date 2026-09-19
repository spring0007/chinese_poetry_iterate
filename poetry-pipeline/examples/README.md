# 接口例子

产线产出三样东西，对应三种接法。挑一条就够了，不必全上。

| 产物 | 体积 | 适用 | 怎么接 |
|---|---:|---|---|
| `dist/poetry.db` | 110.5 MB | 服务端 / 桌面 / iOS / Android / Flutter | 本地 SQLite |
| `dist/bundle/` | 44.0 MB | Web / 小程序 / RN / CDN 按需 | HTTP + zstd |
| `dist/poetry-strains.db`<br>`dist/bundle-strains/` | 13.1 MB<br>4.3 MB | 需要平仄的场景 | ATTACH / 按需分片 |

## 目录

| 文件 | 说明 |
|---|---|
| `api_server.py` | HTTP JSON 接口（标准库，零依赖） |
| `python_usage.py` | Python 库用法：检索 / 详情 / 新增 / 读 bundle / 平仄 |
| `web/index.html` | 浏览器检索 demo（纯前端读 bundle 分片） |
| `js/bundle_query.mjs` | Node / 浏览器读 bundle（需 `npm install`） |
| `sql_examples.sql` | 直接查库的 SQL 片段（各平台通用） |
| `flutter/poetry_client.dart` | Flutter：本地 SQLite + HTTP + bundle 三种接法 |
| `../app.py` | 桌面窗口（检索 / 添加）——不用写代码就能用 |
| `../store.py` | 写入层，GUI / HTTP / CLI 共用；也可 `python store.py add ...` |

---

## 1. HTTP 接口（最快见效）

```bash
cd examples
python api_server.py --db ../dist/poetry.db --strains-db ../dist/poetry-strains.db
# 写接口默认是关的，要新增条目再加 --allow-write
```

```bash
# 健康检查
curl "http://127.0.0.1:8787/api/health"

# 全文检索（繁体查询词会自动转简体）
curl "http://127.0.0.1:8787/api/search?q=鄉愁&limit=3"

# 加过滤：唐诗、体裁=诗、作者=李白
curl "http://127.0.0.1:8787/api/search?q=明月&dynasty=tang&kind=poem&author=李白"

# 详情（挂了平仄包就带 strains 字段）
curl "http://127.0.0.1:8787/api/poem/536870912"

# 作者联想 / 词牌联想
curl "http://127.0.0.1:8787/api/authors?q=李&limit=10"
curl "http://127.0.0.1:8787/api/rhythmics?q=水调"

# 新增（需 --allow-write）
curl -X POST "http://127.0.0.1:8787/api/poem" \
  -H "Content-Type: application/json" \
  -d '{"title":"接口示例","author":"程枢","dynasty":"custom","kind":"poem",
       "lines":["第一句","第二句"],"tags":["示例"]}'
# -> {"id":2023265920,"title":"接口示例","n_char":6}

# 删除（仅限自建条目，删构建期数据会返回 403）
curl -X DELETE "http://127.0.0.1:8787/api/poem/2023265920"
```

响应字段：`id / title / author(佚名为 null) / dynasty / dynasty_label /
kind / kind_label / rhythmic / lines[] / tags[] / score / n_char / src / strains(可选)`

约束（写在服务端，不是靠调用方自觉）：
- 单次 `limit` 上限 200，请求体上限 256 KB
- 默认只听 `127.0.0.1`；要对外改 `--host`，自己确认网络环境
- 写接口默认关闭

---

## 2. Python 库

```bash
python examples/python_usage.py
```

```python
import sys; sys.path.insert(0, ".")
import query as Q, store as ST

con = ST.open_rw("dist/poetry.db", "dist/poetry-strains.db")   # 平仄包可选
rows = Q.search(con, "鄉愁", dynasty="tang", kind="poem", limit=10)
d    = Q.get_by_id(con, rows[0]["id"])       # d["strains_text"] 为平仄
ST.add_poem(con, title="我的诗", author="我", lines=["…"])
```

---

## 3. 浏览器（纯前端，无后端）

```bash
cd dist && python -m http.server 8000
# 打开 examples/web/index.html，把「bundle 目录 URL」填成 http://127.0.0.1:8000/bundle/
```

注意：要用 http 打开，`file://` 下浏览器不允许 fetch 本地文件。
首次按朝代拉取分片（唐诗约 12 MB），之后同源缓存，离线可用。
zstd 解压用 fzstd（CDN 引入），离线环境把 `umd/index.js` 下载到本地改下 `<script src>`。

---

## 4. Node

```bash
cd examples/js && npm install
node bundle_query.mjs info
node bundle_query.mjs search 明月 tang/poem
```

---

## 5. 直接查库

见 `sql_examples.sql`。任何带 SQLite 的平台（iOS/Android/Flutter/桌面/后端）都能直接用，
不需要跑服务。要点：

- 朝代 / 体裁没有列，从 id 高位反解：`dynasty = id>>27`，`kind = (id>>24)&7`
- `author_id = 0` 就是佚名，`LEFT JOIN` 出来是 `NULL`
- 查询词必须先转简体 + 去空格，与库内文本同形
- **导入后务必 `ANALYZE`**，否则 SQLite 会选错索引（`title=?` 从 0.4 ms 变 900 ms）

---

## 6. 平仄（可选包）

```sql
ATTACH DATABASE 'dist/poetry-strains.db' AS strainsdb;
SELECT ps.data, ps.len FROM strainsdb.poem_strains ps WHERE ps.id = 536870912;
```

解包规则：4 bit/符号，`0=仄 1=平 2=，3=。4=○ 5=？6=通 7=未知`；
每字节两个符号、高 4 位在前；末尾若补了 0 nibble，按 `len` 截断。
参考实现见 `schema.unpack_strains`（Python）与 `poetry_client.dart`（Dart）。

---

## 常见坑

| 现象 | 原因 |
|---|---|
| 搜 "乡愁" 有结果、搜 "鄉愁" 没有 | 查询词没做简体归一。库内全是简体，必须先转 |
| 带空格查不到（"帝京篇 十首"） | 库内无空格，查询词同样要去掉空格 |
| 作者搜不到人 | 该姓名在作者库里没有条目，被当成佚名。build.py 已自动补齐 264 个，自建条目走 `ensure_author` |
| `title = ?` 很慢 | 缺 `ANALYZE`，SQLite 选了 `ix_poems_score` 全索引扫描 |
| 新增的条目搜不到 | 不是没写进去，是 `score=0` 排在高热度条目后面。用「作者」检索或在添加窗口列表里找 |
