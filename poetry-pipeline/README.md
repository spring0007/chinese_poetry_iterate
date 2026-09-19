# chinese-poetry 数据重构方案（v3）

把 345.2 MB / 2247 个 JSON 的中国古典文学库，重构成「体积小 + 易检索 + 多平台可消费」的形态。

**v2 的三条硬要求**（均已落地）：① 全库转简体、去空格；② 佚名作者写 NULL；③ 尽量简化、减少重复数据。
**v3 新增的四件事**：④ 平仄做成独立可选包；⑤ 桌面查询 / 编辑 / 添加窗口；⑥ 外接数据扩展点（歌赋、清代诗词、毛选）；⑦ 各平台接口例子。

本文所有数字均来自对本机 `E:\chinese-poetry-master` 的实测，不是估算。

## 零、东西都在哪

工程已放在源库根目录下，`sources.py` 会自动识别源数据位置（不再写死盘符）：

```
E:\chinese-poetry-master\          ← 源库（2247 个 JSON，345 MB）+ PyCharm 项目根
└── poetry-pipeline\               ← 本工程（代码 + 产物）
    ├── dist\                      ← v3 产物：poetry.db / bundle / poetry-strains.db
    ├── .venv\                     ← 专用解释器（装了依赖；app.py 需要 tkinter）
    ├── .idea\runConfigurations\   ← PyCharm 运行配置，打开即有 6 个入口
    ├── requirements.txt
    ├── schema.py sources.py build.py query.py store.py app.py
    ├── external\                  ← 外接数据（歌赋 / 清代诗词 / 毛选模板）
    └── examples\                  ← 各平台接口例子
```

### 在 PyCharm 里用（三步）

1. **打开项目**：`File → Open` 选 **`E:\chinese-poetry-master`**（不是 poetry-pipeline 子目录——
   运行配置里的路径按这个根目录写的）。
   建议把数据目录标记为排除，否则 PyCharm 会给 2247 个 JSON 建索引、明显变卡：
   右键 `全唐诗` / `strains` / `rank` / `御定全唐詩` 等 → `Mark Directory as → Excluded`。
2. **选解释器**：`Settings → Project → Python Interpreter → Add → Existing environment`
   指向 `E:\chinese-poetry-master\poetry-pipeline\.venv\Scripts\python.exe`。
   *必须用它*——系统或别的 venv 里可能没有 tkinter，那样 `app.py` 起不来。
3. **跑**：右上角配置下拉里已经有 6 个现成入口（构建全量 / 校验 / 查询基准 / 桌面窗口 /
   API 服务 / 冒烟构建），直接点绿色三角即可。

### 源数据位置怎么定的

`sources.ROOT` 按此顺序探测，**整个工程可以跟着源库一起搬到任何盘，不用改代码**：

1. 环境变量 `POETRY_SRC`（最优先，临时换个源库时用）；
2. 工程目录的**上一级**（现在的摆法：工程在源库根下）；
3. 工程目录本身；
4. 兜底 `E:\chinese-poetry-master`。

判定依据是"目录里有没有 `strains/` 或 `rank/` 子目录"，不是只看目录存在，避免认错地方。

### 默认路径不依赖工作目录

`build.py / query.py / verify_db.py / size_report.py / store.py` 的默认库路径都按
**脚本文件位置**解析（`__file__` → `dist/`），不用 "./dist"。
why：PyCharm 里右键运行、或从别的目录 `python .../query.py` 时，工作目录不一定是工程根，
写死的相对路径会直接变成"找不到数据库"——实测从 `C:\Users` 运行也能正确找到库。

v1/v2/v3 是**同一套代码的三个迭代版本**，不是一个版本一个文件夹。当前产物就是 v3：

怎么确认手上这份是 v3：

```bash
python -c "import sqlite3;print(sqlite3.connect('dist/poetry.db').execute(\"SELECT v FROM meta WHERE k='schema_version'\").fetchone()[0])"
# -> 3.0
```

| 版本 | schema_version | 区别 |
|---|---|---|
| v1 | 1.0 | 单库含平仄，正文保留繁体 + 另存 `body_s` 简体副本 |
| v2 | 2.0 | 全简体无空格、佚名=NULL、字典化；平仄仍在主库 |
| **v3** | **3.0** | **平仄拆为独立可选包**，新增外接数据与自建号段 |

`schema_version` 是硬约束，不是注释：主版本不一致时 `store.open_ro/open_rw` 直接 `Rejected`，
`query.py` 打 WARN，避免"拿 v2 的库配 v3 代码"这种查得到但语义错的情况。

| | v1 | v2 | **v3** | 变化 |
|---|---:|---:|---:|---:|
| `poetry.db`（主库） | 210.9 MB | 123.6 MB | **110.5 MB** | 再 −10.6% |
| `poetry-strains.db`（平仄包，可选） | 内含 | 内含 | **13.1 MB 单出** | 不装就不占 |
| `bundle/` | 68.1 MB | 48.9 MB | **44.0 MB** | 再 −10.0% |
| `bundle-strains/`（平仄包） | — | — | **4.3 MB** | 可选 |
| 记录数 | 345,390 | 345,354 | 345,359 | +5 条外接 |
| 构建耗时 | 37.1 s | 45.3 s | 44.4 s | — |

只装主库（不要平仄）是 **110.5 MB**，比 v1 少 **47.6%**。

---

## 一、源库体检结论

### 1.1 体积分布（总计 345.2 MB）

| 目录 | 体积 | 占比 | 性质 |
|---|---:|---:|---|
| 全唐诗（含诗人简介） | 133.0 MB | 38.5% | 核心数据：唐 57,607 首 + 宋诗 254,248 首 |
| strains/json（平仄） | 104.1 MB | 30.2% | **派生数据**，7 种符号，与正文逐字 1:1 |
| rank/poet（热度） | 60.6 MB | 17.5% | 搜索引擎命中数，靠字符串关联 |
| 御定全唐詩 | 19.7 MB | 5.7% | **异文版本**，与全唐诗标题交集 69.8% |
| 全唐诗/error | 8.9 MB | 2.6% | 脏数据，构建时排除 |
| 宋词 | 8.8 MB | 2.6% | 21,053 首 |
| 元曲 | 4.0 MB | 1.2% | 11,057 首 |
| 其余 12 个集合 | ~6.2 MB | 1.8% | 诗经/楚辞/论语/蒙学/纳兰性德等 |

### 1.2 四个结构性问题

**① 平仄占了 30% 却只用 7 种符号**
实测 365 万个符号中：`仄` 548,858 / `平` 509,299 / `，` 102,176 / `。` 101,541 / `○` 64,678 / `？` 1,395 / `通` 3。
UTF-8 每个汉字 3 字节 → 4 bit 足够。实测：10.46 MB → 4bit 打包 1.75 MB（−83.3%）→ +zstd19 **0.50 MB**（−95.2%）。

**② UUID 主键浪费 10.71 MB 且无法范围定位**
311,855 首诗各带一个 36 字符 UUID，零重复，纯开销。更糟的是 UUID 无顺序性，
要找「所有宋诗」只能遍历文件名分片，无法按 id 范围直接定位。

**③ 简繁混杂，且不归一等于搜不到（这是最致命的）**

| 集合 | 与简体差异率 | 判定 |
|---|---:|---|
| 唐诗 | **27.82%** | 繁体源 |
| 宋诗 | **26.65%** | 繁体源 |
| 御定全唐詩 | 27.51% | 繁体源 |
| 宋词 | 0.22% | 简体源 |
| 元曲 | 0.04% | 简体源 |
| 诗经 | 0.45% | 简体源 |
| 楚辞 | 0.72% | 简体源 |

铁证（v1 实测）：搜「乡愁」命中 **0** 首，搜「鄉愁」命中 **40** 首。
同一个库里两套字形，用户无论输入哪种都会漏一半。**v2 已解决**：全库归一为简体，
查询词用同一个函数归一，实测 `鄉愁` 与 `乡愁` 返回完全一致的结果集。

**④ 313 个分片文件 + 无 id 的关联表**
`poet.song.0.json … poet.song.254000.json` 共 255 个分片，随机访问必须先猜分片。
`rank/` 60 MB 完全没有 id，只能靠 `(author, title)` 字符串对齐——这是脆弱假设，必须校验。

### 1.3 收益极小的方向（别在这里浪费时间）

- **内容去重**：`(author, title, 正文)` 完全重复仅 82 / 345,441 条 = **0.024%**。
  该做的还是要做（避免检索出两遍同样的诗），但别指望它省体积。
- **给低重复列建字典表**：见 §3.3，标题就是反例，字典表反而多花 29 MB。

---

## 二、检索方案选型（三个方案全量实测对比）

| 方案 | 数据库体积 | 相比无索引 | 2 字查询 | 长句查询 | 构建耗时 | 结论 |
|---|---:|---:|---|---:|---:|---|
| LIKE 全表扫描（默认） | **110.5 MB** | — | ✅ 4–187 ms | 未命中 ~1.0 s | 0 | **采用** |
| FTS5 trigram | 462.9 MB | **+339 MB** | ❌ 命中 0 | ✅ | +21.1 s | 否决 |
| 正文 bigram 倒排 | ~790 MB | +666 MB | ✅ 0.1 ms | ✅ | +230 s | 否决 |

两个必须记住的坑：

1. **FTS5 trigram 不支持 < 3 字查询**。实测「明月」LIKE 命中 50 / FTS 命中 0，「梅花」LIKE 50 / FTS 0。
   诗词检索里 2 字词（明月、乡愁、梅花、秋风）恰恰是最高频的。
2. **FTS5 trigram 不做繁简归一**，且索引体积是正文的 ~4.5 倍。

### 2.1 v3 实测延迟（`python query.py --db ./dist/poetry.db bench`）

| 查询 | 类型 | 命中 | 延迟 |
|---|---|---:|---:|
| 明月 | 2 字高频 | 20 | 15.5 ms |
| 梅花 | 2 字高频 | 20 | 7.4 ms |
| 秋風 | 2 字**繁体输入** | 20 | **4.2 ms** |
| 乡愁 | 2 字需繁转简 | 20 | 180.9 ms |
| 千里共婵娟 | 5 字名句 | 8 | 988.5 ms |
| 不存在的词 | 未命中全扫 | 0 | 985.9 ms |

P50 = 98 ms，P95 = 988 ms。

次级索引路径（确认索引真的被用上，不是白占空间）：

| 路径 | 命中 | 延迟 | 走的索引 |
|---|---:|---:|---|
| 标题等值 `静夜思` | 1 | **0.3 ms** | `ix_poems_title` |
| 标题子串 | 11 | 0.3 ms | 退化为主表扫描 |
| 词牌 `水调歌头` | 20 | 1.8 ms | `rhythmics` 唯一索引 + `ix_poems_rhythmic` |
| 作者 `李白` | 20 | 1.1 ms | `ix_authors_name` + `ix_poems_author` |

**一个必须记住的坑**：SQLite 没有 `ANALYZE` 统计信息时，会为了吃掉 `ORDER BY score DESC`
而选 `ix_poems_score` 做全索引扫描，把 `p.title = ?` 这种高选择性查询拖到 **910 ms**。
加 `ANALYZE` 后降到 **0.4 ms**。构建流程末尾已固定执行 `ANALYZE`（代价几 KB）。

**应用侧建议**：加一层查询结果缓存（LRU）。高频词已经毫秒级，冷门词的 1 s 全扫靠缓存就能抹平。

---

## 三、数据格式设计

### 3.1 统一记录 Schema（v3）

```
id        int32   (dynasty<<27)|(kind<<24)|seq   高4位朝代/中3位体裁/低24位序号
title     string  简体、无空格
author    string  简体、无空格；佚名为 ""（落库 author_id=0，即 NULL）
body      string  简体、无空格，由 lines 用 \n 连接；兼作展示文本与检索列
rhythmic  string  词牌 / 曲牌，诗为空
tags      []string  三百首系列回填的分类标签
notes     []string  注释
strains   bytes   4bit/字打包的平仄，与正文逐字 1:1（只在平仄包里）
score     uint8   热度 0-255，由 rank 五个引擎命中数 log 量化
src       string  来源集合，便于定位脏数据
```

**相对 v1 删掉的三样**：
- `body_s`（简体副本）—— 全库已归一为简体，正文即检索列，不需要第二份；
- `dynasty` / `kind` 两列 —— id 高位已编码，用 `parse_id()` 反解；
- `titles` 字典表 —— 见 §3.3，它是净亏损。

**v3 起 `strains` 移出主库** —— 见 §3.4。

**id 编码的价值**：给定 id 即可反解出朝代与体裁，无需额外索引就能做
「只查唐诗」「只查宋词」这类范围过滤；且可安全存进 int32，比 36 字符 UUID 省 10.71 MB。

v3 扩充的编码位（为外接数据预留）：

| 朝代 | 码 | | 体裁 | 码 |
|---|---:|---|---|---:|
| unknown/preqin/han/wudai | 0–3 | | poem / ci / qu | 0–2 |
| tang/song/yuan/ming/qing | 4–8 | | prose / classic | 3–4 |
| modern | 9 | | **fu（赋）** / **article（论著）** | **5–6** |
| **custom（自建，GUI/API 新增）** | **15** | | | |

`custom` 占最高位（15）是刻意的：GUI / API 新增的条目与构建期产物共用同一条查询路径，
不需要在 `search()` 里 union 两张表，也不会和构建期 id 撞号（seq 从 10,000,000 起）。

### 3.2 三种关联数据的处理

| 数据 | 原形态 | 处理 | 体积变化 |
|---|---|---|---:|
| 平仄 | 104.1 MB JSON，7 种符号 | 4bit 打包 + zstd | → 13.11 MB（独立包） |
| 热度 | 60.6 MB，4 个 int/条 | log 量化成 1 个 uint8 | → 0.3 MB |
| 御定全唐詩 | 19.7 MB 异文 | 独立可选包，默认不进主库 | 按需 |

挂接覆盖率（全量）：平仄 唐诗 100.0% / 宋诗 100.0%；热度 唐诗 98.7% / 宋诗 100.0% / 宋词 95.5%。

### 3.3 「减少重复数据」的正确做法：先量再改

字典化不是万能的。**判定标准是「重复率」= 记录数 / 唯一值数**：

| 列 | 唯一值 | 重复率 | 逐行存字符串 | 决策 |
|---|---:|---:|---:|---|
| 来源 src | 17 | 20,315.2 | 2.91 MB | ✅ 字典化 |
| 词牌 rhythmic | 1,463 | 236.1 | 0.07 MB | ✅ 字典化 |
| 作者 author | 13,439 | 25.7 | 0.82 MB | ✅ 字典化（且作者还有简介要挂） |
| **标题 title** | **273,139** | **1.26** | 2.71 MB | ❌ **直接存字符串** |

**标题是 v1 踩过的坑**：字典表省不下数据（重复率只有 1.26），却要多付
9.80 MB 表 + 9.77 MB 唯一索引 + 9.77 MB 冗余索引 = **29.3 MB**，比逐行存的 2.71 MB 贵十倍。
v1 里还额外给 UNIQUE 列重复建了普通索引（`ix_titles_name` 与 `sqlite_autoindex_titles_1`
内容完全一样），纯浪费，v2 已删除。**凡是声明了 UNIQUE 的列，不要再手动建同名索引。**

### 3.4 平仄独立可选包（v3）

| | 主库含平仄（v2） | **主库 + 平仄包（v3）** |
|---|---:|---:|
| 只要有正文的用法 | 123.6 MB | **110.5 MB** |
| 需要平仄的用法 | 123.6 MB | 110.5 + 13.1 = 123.6 MB |
| Web 端按需 | 48.9 MB | **44.0 MB**（平仄另 4.3 MB） |

平仄只覆盖唐诗 / 宋诗（311,786 条 = 全库 **90.3%**），而检索、列表、卡片这些高频路径
根本用不到它。塞进主库等于让所有不需要平仄的用法都多背 13 MB。

```bash
python build.py --out ./dist                  # 只出主库 + bundle（110.5 / 44.0 MB）
python build.py --out ./dist --with-strains   # 额外出 poetry-strains.db + bundle-strains
```

平仄包是一张单表 `poem_strains(id, data, len)`，按 id 与主库 1:1 关联。查询侧用 ATTACH 挂载：

```bash
python query.py --db ./dist/poetry.db --strains-db ./dist/poetry-strains.db get 536877844
python query.py --db ./dist/poetry.db bench                    # 不挂载，详情里平仄为 null
```

`verify_db.py` 会断言「平仄包按 id 全部可关联主库」（311,786 / 311,786）。
**没挂平仄包时 `has_strains()` 返回 False，不会报错** —— 平仄永远是可选增强，不是查询前置条件。

### 3.5 双产物

同一套规范化中间层产出两种形态，覆盖不同平台：

- **`poetry.db`（SQLite）** — 服务端 / 桌面 / iOS / Android / Flutter。SQL 直接查，随机访问 O(1)。
- **`bundle/`（JSONL 分片 + zstd）** — Web / 小程序 / 离线包 / CDN。天然支持按需下载子集。

bundle 构成（全量，v3）：

```
shards/   184 个 zstd 分片   43.9 MB
index/    作者 / 标题 / 词牌 三个索引 + 枚举
bundle-strains/   79 个分片   4.31 MB（仅 --with-strains）
```

按集合拆看体积（按需下载的直接依据）：

| 集合 | 条数 | 分片体积 |
|---|---:|---:|
| 宋诗 song/poem | 254,193 | 32.6 MB |
| 唐诗 tang/poem | 57,741 | 7.6 MB |
| 宋词 song/ci | 21,049 | 2.8 MB |
| 元曲 yuan/qu | 10,906 | 1.5 MB |
| 其余 13 个集合 | 1,470 | < 0.8 MB |

**只想装唐诗就只下 `tang-poem-*.jsonl.zst`（7.6 MB）** —— 这是 SQLite 单文件做不到的。

---

## 四、桌面窗口：查询 / 编辑 / 添加（v3）

```bash
python app.py                                  # 双击即开，默认读 ./dist/poetry.db
python app.py --db ./dist/poetry.db --strains-db ./dist/poetry-strains.db
```

- **检索页**：关键词 / 朝代 / 体裁 / 作者 / 词牌 组合过滤，结果表 + 详情面板（挂载平仄包时正文下逐行显示平仄）。
  **结果分页**：每页条数可调（默认 50），底部有「首页 / 上一页 / 下一页 / 末页 / 跳页」与「第 X / Y 页（共 N 条）」——
  命中几千条也能逐页翻看，不再受"只显示 50 条"限制。
  结果表新增「核对」列（✓ 表示该诗已核对），选中一条可点「编辑诗词」订正、「编辑作者」改作者简介/朝代、
  「标记已核对 / 取消」切换核对状态、或「删除选中」删除；**右键**结果行也有同样菜单。
- **核对状态（新增）**：一张独立 sidecar 表 `poetry-checks.db`，记录每首诗词是否已被人工核对（`checks(poem_id, verified, checked_at, note)`）。
  why 单独成库：核对标注是用户行为数据，和"从源码重建出来的诗词"生命周期不同，放进主库会被 `build.py` 重建清掉；独立文件永远不被 build 触碰，**重跑 build 自动保留**。
  窗口**最底部状态栏**始终显示「核对状态：已核对 X 篇 / 待核对 Y 篇（全库共 Z 篇）」；详情区也列出当前诗的核对状态；选中后按钮一键切换。
  CLI：`python store.py --db ./dist/poetry.db check set <id> 1`、`check get <id>`、`check count`。
- **添加页**：录入新条目，正文按行写。提交后**立即可被检索到**——文本走与构建期完全相同的归一规则，
  繁体自动转简体、空格自动去除。下方「我的条目」列表可编辑 / 删除自己添加的或修订过的条目。
- **编辑诗词（修订）**：选中任意一条（含构建期导入、显示有误的诗词），弹窗预填标题/作者/词牌/标签/注释/正文，
  改完保存即写回数据库。朝代、体裁**不可改**——它们编码在 id 高位，改了会变 id、破坏平仄关联与既有检索链接。
  修订过的构建期数据会被标记为 `src='user-edit'`，与原始构建数据区分（见 `store.update_poem`）。
- **编辑作者**：在详情或右键菜单点「编辑作者」，弹窗可单独改**作者名 / 朝代 / 简介（小传）**并写回 `authors` 表。
  作者朝代是独立于诗词的元数据（诗词朝代来自 id 高位），改作者简介**不会**动到诗词本身的朝代/体裁，也不影响检索与平仄关联。

设计上几个刻意的决定：

| 决定 | 理由 |
|---|---|
| 用 Tkinter（标准库） | 零第三方依赖，Windows/macOS/Linux 都能跑，单文件双击即开。界面朴素，但这是个"查数据、录数据"的工具，省掉整条前端构建链更划算 |
| 检索放子线程 + 分页 | 未命中查询要全表扫 ~1 s，放主线程会把窗口卡死；分页用 `run_query` 一次取 `COUNT(*)` 和当前页 `LIMIT/OFFSET`，翻页只重查偏移量，不重扫全表 |
| **构建期原始数据默认删不掉，但能改，也能强制删** | 删除默认只认 `src IN ('user-added','user-edit')`——误点不会把唐诗删掉；显示有误的诗词可以修订（`update_poem` 保留 id 原地订正）。若确实要删一条原始数据，删除时会二次确认「强制删除」（删除后只能重建数据库恢复）。注意：重跑 `build.py` 会从源码重新生成、覆盖修订，修订前的修正请用 `store export-edits` 备份 |
| **编辑不改 id / 朝代 / 体裁** | id 高位编码朝代+体裁，改了 id 会变，poetry-strains.db 的平仄关联与既有检索链接都会断；所以修订只动文本/作者类字段 |
| **作者简介/朝代单独编辑** | 作者档案在 `authors` 表，与诗词通过 `author_id` 关联；改作者朝代/小传只改这一行，所有指向它的诗一起显示新信息，且不动诗词 id |
| 正文没命中时兜底查标题 | `search()` 只扫 body（《静夜思》根本不在正文里），不兜底的话用户输入标题会得到 0 结果，看起来像库里没这条 |

### 4.1 两个只在真实运行才暴露的坑（都是实测抓出来的）

**① Tk 跨线程调用不生效。** 实测（Python 3.14.2 / Tk 8.6）：子线程里调 `root.after(0, cb)`，
主线程 `update()` 泵事件 **11.6 秒也没触发**；子线程写 `StringVar` 同样无效；只有主线程调度的 `after()` 会执行。
表现是界面永远停在「检索中…」。→ 改为：子线程只把结果放进 `queue.Queue`，主线程用 `after(30, self._pump)` 轮询取回。

**② sqlite3 连接不能跨线程复用。** 子线程用主线程创建的连接会抛
`ProgrammingError: SQLite objects created in a thread can only be used in that same thread`。
这个报错被 ① 掩盖过——结果回调根本不触发，所以看不到。
→ 改为：子线程每次检索用 `ST.open_ro()` 开自己的只读连接（`mode=ro`），用完即关。
**HTTP 服务同款问题**（`ThreadingHTTPServer` 多线程共用一个连接），已一并改成每请求独立连接。

---

## 五、写入层 store.py（GUI / HTTP / CLI 共用）

新增条目要同时维护 `poems` / `authors` / `rhythmics` / `sources` 四张表和 id 号段，
任何一处漏掉都会留下「查得到正文但按作者搜不到」的半成品数据，所以收敛成一层：

```bash
python store.py --db ./dist/poetry.db add --title "静夜思" --author 李白 \
                --dynasty tang --kind poem --body "床前明月光|疑是地上霜"
python store.py --db ./dist/poetry.db list
python store.py --db ./dist/poetry.db rm 2023265920
python store.py --db ./dist/poetry.db stats
# 修订一条（保留 id，不改朝代/体裁；不传的字段保留原值）
python store.py --db ./dist/poetry.db edit 134217728 --title "新标题" --author 杜甫
# 把修订过的构建期数据导出备份（重跑 build.py 会覆盖它们）
python store.py --db ./dist/poetry.db export-edits ./my-edits.jsonl
```

### 5.1 重建不再丢数据（v3.1 起自动保全）

旧版里 `build.py` 是整库重建，跑一次自建 / 修订条目全没。现在 `build.py` 在重建前会**自动快照** `poetry.db` 里的用户数据
（自建 + 修订，含其作者 / 词牌 / 来源），重建后再自动重放恢复，**保留原 id**，无需手工导出导入。

- 自建条目（`user-added`）：重建后原样恢复，custom 号段不与构建期冲突。
- 修订条目（`user-edit`）：用原 id 覆盖重建后的同 id 行——即"你的订正依然生效"。
- 作者 / 词牌按名复用新库字典，找不到才新建；恢复后用 `GROUP BY` 重算 `authors.n_poems`，计数一致。
- **核对标注**（`poetry-checks.db`）是独立 sidecar，根本不被 `build.py` 触碰，重建前后一直保留。

> 跨主版本（schema 大版本号变化）时不会自动保全——结构可能不兼容，此时会打印告警，请改用下面的导出导入手动备份。

仍建议定期 `export` / `export-edits` 作额外保险（尤其换机器 / 换目录前）：

```bash
python store.py --db ./dist/poetry.db export ./my-poems.jsonl     # 自建条目
python store.py --db ./dist/poetry.db export-edits ./my-edits.jsonl  # 修订备份
python build.py --out ./dist --with-strains                     # 重建（自动保全）
python store.py --db ./dist/poetry.db import ./my-poems.jsonl   # 仅在自动保全未覆盖时手动补回
```

导入默认按 `(title, body)` 去重，重复导入不会产生重复行（实测：写入 0、跳过 2）。
删除条目会回退 `authors.n_poems`，否则删几次之后作者作品数会一直虚高。

---

## 六、外接数据扩展点：歌赋 / 清代诗词 / 毛选（v3）

`external/` 目录下的 JSON 会自动被构建流程摄入，不用改任何 Python 代码：

```
external/
├── fu.json            歌赋：洛神赋、阿房宫赋、前赤壁赋（3 条，已入库）
├── qing-poetry.json   清代诗词：纳兰性德、龚自珍、赵翼（2 条示例，已入库）
├── mao-xuan.json      毛选：模板已建好，enabled=false（未录入内容）
└── README.md          字段格式说明
```

每条记录的格式：

```json
{
  "enabled": true,
  "name": "fu",
  "dynasty": "song",          // 可用 S.DYNASTY 里的任一朝代；也可逐条覆盖
  "kind": "fu",               // 可用 S.KIND 里的任一体裁；也可逐条覆盖
  "items": [
    {"title": "前赤壁赋（节选）", "author": "苏轼", "lines": ["...", "..."],
     "dynasty": "song", "kind": "fu", "tags": ["赋"], "notes": ["..."]}
  ]
}
```

关键约束：**外接数据同样要过简体归一 / 去空格**，否则等于开了个后门绕过归一规则。
`verify_db.py` 里有专门一项断言（异常 0 条）。

**毛选怎么加**：`kind` 用新增的 `article`（论著），朝代用 `modern`。
非韵文没有 title 时会用首行前 12 字兜底；按段落拆成 `lines` 即可，检索行为与诗词完全一致。
把 `mao-xuan.json` 的 `enabled` 改成 `true`、填 `items` 就会进下一次构建。

---

## 七、接口例子（v3）

```
examples/
├── api_server.py       HTTP JSON 接口（标准库，零依赖）
├── python_usage.py     Python 直接调用的常见姿势
├── sql_examples.sql    常用 SQL 片段（含号段过滤、字典表 JOIN）
├── web/index.html      纯前端检索 Demo（读 bundle 分片 + fzstd 解压）
├── js/bundle_query.mjs Node / 浏览器通用的 bundle 读取器
├── flutter/poetry_client.dart   Flutter（sqflite）客户端
└── README.md           各例子的运行方式
```

启动服务：

```bash
python examples/api_server.py --db ./dist/poetry.db \
       --strains-db ./dist/poetry-strains.db --port 8787
python examples/api_server.py --db ./dist/poetry.db --allow-write   # 开放写接口
```

| 接口 | 说明 |
|---|---|
| `GET /api/health` | 存活 + 是否挂平仄包 + 是否可写 |
| `GET /api/stats` | 条数 / 作者数 / 自建数 |
| `GET /api/search?q=明月&dynasty=tang&kind=poem&author=&limit=10&offset=0` | 正文检索（繁体输入自动转简） |
| `GET /api/poem/<id>` | 详情，挂了平仄包则带 `strains` |
| `GET /api/authors?q=李` / `GET /api/rhythmics?q=水调` | 联想 |
| `GET /api/mine` | 自建条目 |
| `POST /api/poem` / `DELETE /api/poem/<id>` | 需 `--allow-write`；删除仅限自建 |

**默认只读是刻意的安全默认值**：对外暴露的服务不该能改数据，写接口单独开关。

端到端实测（`python smoke_api.py`）：全部端点 200/201/400/403 符合预期；
**20 并发请求失败 0 个**（P50 13 ms / P95 534 ms）——这是跨线程连接修复的验证；
繁简等价（鄉愁/乡愁 结果一致）；`limit=999999` 被夹到 200；删除构建期数据返回 403。

---

## 八、代码架构

```
poetry-pipeline/
├── schema.py       统一数据模型：SCHEMA_VERSION、id 编解码、平仄 4bit 打包、热度量化、
│                   norm_text（简体归一，跑到不动点）/ norm_author（佚名置空）
├── sources.py      源数据注册表：16 个内置集合 + external/ 外接集合的字段映射、
│                   分片自然排序、嵌套正文展平 _flatten()、标题兜底 pick_title()、
│                   整集作者补录（所有"脏"兼容逻辑收敛在这一层，加新集合只改这张表）
├── build.py        ETL 主流程：读取 → 挂接 → 归一 → 去重 → 产出 → ANALYZE
│                   （--with-strains 额外出平仄包）
├── query.py        查询层：search / author / title / rhythmic / get / run_query（含分页 COUNT+OFFSET）/ bench
│                   （--strains-db 按需 ATTACH 平仄包）
├── store.py        写入层：add / edit / update_author / rm / list / stats / export / import（GUI+HTTP+CLI 共用）
├── app.py          桌面窗口：检索页 + 编辑弹窗 + 添加页（Tkinter，零依赖）
├── verify_db.py    断言式校验：三条硬要求 + 数据质量 + 平仄包关联 + 外接数据，失败非零退出
├── size_report.py  体积账：字典化是否划算、库内页占用，数字可复现
├── smoke_gui.py    窗口冒烟：真构造一遍窗口，录入→落库→删除→并发检索
├── smoke_store.py  写入层冒烟：导出/导入闭环、繁体归一、删除权限
├── smoke_api.py    接口冒烟：起真服务打全部端点 + 20 并发
├── external/       外接数据（歌赋 / 清代诗词 / 毛选模板）
└── examples/       各平台接口例子
```

**分层理由**：源库正文有 4 种字段名（`paragraphs` / `content` / `para` / `spells`）、
作者有 2 种、有无 id 不一致。若把兼容逻辑散进主流程，每加一个集合就要改主流程。
注册表模式让主流程只消费统一记录。

### 构建与校验

```bash
pip install -r requirements.txt           # zstandard + zhconv，构建/查询/校验 都要

# app.py 额外需要 tkinter，它由解释器自带、pip 装不了。
# 若当前解释器没有 tkinter（常见于某些 venv / Linux），用带 tkinter 的解释器建工程内 venv：
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt

python build.py --out ./dist --with-strains         # 全量，约 44 s
python build.py --out ./dist --limit 3000           # 冒烟：每集合只取前 N 条，约 6 s
python build.py --out ./dist --with-optional        # 额外打包御定全唐詩
python build.py --out ./dist --with-fts             # 开 FTS5（体积 +339 MB 且不支持 2 字查询）

python verify_db.py ./dist/poetry.db ./dist/poetry-strains.db   # 断言全绿才算构建合格
python size_report.py ./dist/poetry.db              # 体积账

python query.py --db ./dist/poetry.db --strains-db ./dist/poetry-strains.db search 乡愁
python query.py --db ./dist/poetry.db search 鄉愁   # 繁体输入，结果一致
python query.py --db ./dist/poetry.db bench         # P50/P95 + 索引路径验证

python smoke_store.py                               # 写入层（导出/导入闭环）
python smoke_api.py                                 # 接口（起真服务 + 并发）
python smoke_gui.py                                 # 窗口（需要 tkinter）
```

---

## 九、多平台接入矩阵

| 平台 | 推荐产物 | 检索方式 | 关键点 |
|---|---|---|---|
| 服务端 API（Python/Go/Node） | `poetry.db` | `LIKE body` + 作者/标题/词牌索引 | 加一层 LRU 缓存，未命中查询 ~1 s |
| 桌面（Tkinter / Electron / Tauri） | `poetry.db` | 同上 | 本项目 `app.py` 即为可直接用的 Tkinter 实现 |
| iOS / Android 原生 | `poetry.db` | 同上 | 系统自带 SQLite |
| Flutter | `poetry.db` | 同上 | sqflite，见 `examples/flutter/` |
| Web | `bundle/` | 分片加载 + JS 过滤；全文检索走服务端 API | zstd 解压用 fzstd（~50 KB wasm） |
| 微信小程序 | `bundle/` | 只下需要的分片 + 索引；全文走服务端 API | 小程序无 SQLite，必须走 bundle |
| 静态站 / CDN | `bundle/` | HTTP Range 按分片拉取 | 天然支持增量更新 |

**为什么 Web / 小程序不做本地全文检索**：自建正文倒排索引实测 +666 MB，完全不现实。
正确做法是把全文检索放在服务端，客户端只做「作者 / 标题 / 词牌」这类轻量索引
（bundle 自带，压缩后 3.67 MB）。

---

## 十、健壮性设计

| 风险点 | 处理方式 |
|---|---|
| **zhconv 单次转换不幂等** | `norm_text` 跑到不动点（最多 3 轮）。词组级最长匹配会把 `餘慶` 先落到异体 `馀庆`，再转一轮才到 `余庆`；只转一次会在库里留 0.14% 异体字。不变量 `convert(x)==x` 由 `verify_db.py` 断言 |
| 作者字典只覆盖唐/宋/五代 | 建 id 表时把正文里出现、作者库没有的姓名补齐（本次 264 个：曹操、关汉卿、东方朔…）。**否则这些人的名字会被静默丢成 0＝佚名** |
| rank 无 id，靠字符串对齐可能挂错 | 逐条校验 `(author,title)`，覆盖率 98.7%~100%，不足 90% 时告警。**绝不静默挂错** |
| 分片文件名字典序会导致关联错位 | `natural_key()` 按数字排序，不依赖目录顺序 |
| 单文件 JSON 解析失败 | 抛异常由调用方决定跳过还是中止，不静默丢数据，丢弃数在日志里显式打印 |
| 宋词源数据没有标题字段 | `pick_title()` 按 标题 → 词牌 → 首行前 12 字 兜底，保证无空标题 |
| 蒙学正文是嵌套 JSON | `_flatten()` 递归展开，否则正文会变成 `{'chapter':...}` 这种垃圾 |
| 平仄与正文长度不匹配 | 存 `strains_len`，解包时按原长度截断，不靠猜测 |
| seq 超出 24 位 | `make_id()` 直接抛错，不静默回绕产生重复 id |
| SQLite 选错索引 | 构建末尾固定 `ANALYZE`；`bench` 子命令会打印次级索引路径的实测延迟 |
| **Tk 跨线程调用不生效** | 子线程结果进 `queue.Queue`，主线程 `after()` 轮询取回（见 §4.1） |
| **sqlite 连接跨线程** | 每次检索 / 每个请求开独立只读连接（`mode=ro`），用完即关（见 §4.1） |
| **重建清空自建 / 修订条目** | v3.1 起 `build.py` 自动快照+恢复用户数据（保留 id），无需手工导出；跨主版本不自动保全，仍可用 `store.py export` / `export-edits` 作保险（见 §5.1） |
| **核对标注因重建丢失** | 核对表放进独立 sidecar `poetry-checks.db`，`build.py` 永不触碰，重建前后自动保留 |
| **误删构建期数据** | `delete_poem()` 默认只认 `src IN ('user-added','user-edit')`，其余一律 `Rejected`；确要删原始数据须 GUI 二次确认 `force=True` |
| 删除后作者作品数虚高 | `delete_poem()` 回退 `authors.n_poems` |
| **库版本与代码版本不一致** | `meta.schema_version` 主版本不符时 `open_ro/open_rw` 直接 `Rejected`，`query.py` 打 WARN；`verify_db.py` 把版本戳列为第一项断言 |
| HTTP 服务被大 body 打爆 | `MAX_BODY = 256 KB`，`MAX_LIMIT = 200`，`timeout = 30 s` |

---

## 十一、三条硬要求的落地结果

### 要求 1：全库简体 + 无空格

| 项 | 结果 |
|---|---|
| 归一耗时 | 20.9 s（345,441 条，跑到不动点） |
| 标题去空格 | 126,558 条 |
| 残留繁体（抽样 8000 条） | **0** |
| 正文 / 标题 / 作者名含空格 | **0 / 0 / 0** |
| 繁体输入 `鄉愁` vs `乡愁` | 结果集完全一致，各 20 条 |
| 外接数据（歌赋 / 清代诗词） | 同样已归一，异常 **0** 条 |

### 要求 2：佚名写 NULL

| 项 | 结果 |
|---|---|
| 判定规则 | 转简体后，精确匹配 `{不详, 佚, 匿名, 无名, 失名, 阙名…}` 或含 `{佚名, 无名, 失名, 阙名}` 子串 |
| 本次置空 | 5,871 条（源库里佚名写法有 111 种：无名氏 3,502 / 不詳 886 / 無名氏 875 …） |
| 库里 `author_id=0` | 6,205 条（1.8%），JOIN 后确实为 NULL |
| `authors` 表里佚名占位条目 | **0**（不为「无名氏」建条目） |
| 不误杀 | 保留 62 个「某氏」真名（王氏、花蕊夫人徐氏…），这些是以姓氏指代的实名 |

作者为空的分布（区分「真佚名」与「该集合本来就没录作者」）：

| 集合 | 作者为空 | 说明 |
|---|---:|---|
| 诗经 / 论语 / 四书五经 | 100% | 源数据无作者字段，属实 |
| 元曲 | 21% | 源数据部分缺失 |
| 宋词 | 7% | 多为民间无名氏词作 |
| 唐诗 | 3% | |
| 宋诗 | 0% | |
| 曹操 / 幽梦影 / 纳兰性德 | 0% | 整集缺作者字段，已按集合补录 |

### 要求 3：简化与去重

| 手段 | 省下 |
|---|---:|
| 去掉 `body_s` 简体副本（正文即简体） | 一整份正文 = 64.55 MB |
| `dynasty` / `kind` 不落库（id 高位反解） | ~3.29 MB + 两个索引 |
| 作者 / 词牌 / 来源 字典化 | ~3.5 MB + 字符串索引降为整数索引 |
| 标题**不**字典化（v1 的错） | 省回 ~29.3 MB |
| 删掉与 UNIQUE 重复的冗余索引 | ~9.8 MB |
| **平仄移出主库（v3）** | **再省 13.1 MB** |
| 内容去重 | 82 条 |

合计：**210.9 MB → 110.5 MB**（−47.6%）。

库内页占用（`size_report.py` 实测）：

| 对象 | 体积 |
|---|---:|
| `poems`（含正文 64.55 MB） | 83.96 MB |
| `ix_poems_title` | 11.51 MB |
| `ix_poems_author` / `ix_poems_score` / `ix_poems_rhythmic` | 3.95 / 3.92 / 3.34 MB |
| `authors` | 3.45 MB |
| ~~`poem_strains`（平仄）~~ | ~~13.09 MB~~ → v3 已移出主库 |
| 其余 | < 0.4 MB |

**还能再压吗**：正文占主表 100%，正文之外已无可观冗余。不建议再动结构。

---

## 十二、实测结果（v3 全量构建）

```
源库 JSON          345.2 MB / 2247 个文件
  ↓ 构建 45.6s
poetry.db          110.5 MB  （345,359 条 + 13,755 作者 + 热度）
poetry-strains.db   13.1 MB  （311,786 条平仄，可选）
bundle/             44.0 MB  （184 个 zstd 分片 + 索引）
bundle-strains/      4.3 MB  （79 个分片，可选）
```

| 环节 | 结果 |
|---|---|
| 读入 | 345,441 条（含外接 5 条）；空正文丢弃 170 条 |
| 去重 | 丢弃 82 条 → 345,359 条 |
| 平仄挂接 | 唐诗 100.0% / 宋诗 100.0%（按 UUID） |
| 热度挂接 | 唐诗 98.7% / 宋诗 100.0% / 宋词 95.5%（按内容键） |
| 简体归一 | 佚名置空 5,871 条；标题去空格 126,558 条；耗时 20.9 s |
| 作者补齐 | 正文出现、作者库缺失的姓名 264 个 |
| 标签增强 | 从三百首系列回填 259 条 |
| 校验 | `verify_db.py` 全部断言**通过** |
| 窗口冒烟 | `smoke_gui.py` 录入→归一→落库→删除→并发检索，全通过 |
| 接口冒烟 | `smoke_api.py` 全端点 + 20 并发，失败 0 |
| 写入冒烟 | `smoke_store.py` 导出/导入闭环 + 去重，全通过 |

---

## 十三、待确认清单

1. **幽梦影的作者补录为「张潮」**：源数据整集没有作者字段，按常识补录。
   `[待核实]`：若要求严格只认源数据，在 `sources.py` 里把 `Collection("youmengying", …)`
   的 `default_author="张潮"` 删掉即可回退为 NULL。
2. **源库里的缺字记法 `{𥫗/戢}` / `{枦睘}` 原样保留**：这是源库用「两个部件拼一个缺字」的写法，
   不是脏数据，未做替换（替换等于造字）。全库约数十处。
3. **絪 → 𬘡**：zhconv 会把个别字转成 CJK 扩展区字符（U+2C621）。源库本身已含扩展区字符
   （如「𡔹㚃」），故未特殊处理；若目标端字体不支持，可在 `norm_text` 里加一条
   「转换后超出 BMP 则保留原字」的规则。
4. **FTS5 / bigram 的体积与延迟**为 v1 时期的实测值（254,248 条规模），v2/v3 未重跑——
   结论方向不受影响（trigram 不支持 2 字查询是 tokenizer 的固有行为）。
5. **`external/mao-xuan.json` 目前 `enabled=false`**：毛选内容涉及版权，未预置正文。
   模板与字段说明已就绪，填入 `items` 并把 `enabled` 改成 `true` 即可入库。
6. **`app.py` 依赖 tkinter**：托管 venv 里没有，需用系统 Python 建 venv（见 §八）。
   `[待核实]`：若要打包成 exe，`pyinstaller -F app.py` 未实测。
