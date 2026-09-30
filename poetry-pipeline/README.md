# chinese-poetry 数据重构方案（v3）

把 345.2 MB / 2247 个 JSON 的中国古典文学库，重构成「体积小 + 易检索 + 多平台可消费」的形态。

**v2 的三条硬要求**（均已落地）：① 全库转简体、去空格；② 佚名作者写 NULL；③ 尽量简化、减少重复数据。
**v3 新增的四件事**：④ 平仄做成独立可选包；⑤ 桌面查询 / 编辑 / 添加窗口；⑥ 外接数据扩展点（歌赋、清代诗词、毛选）；⑦ 各平台接口例子。

本文所有数字均来自对本机 `E:\chinese_poetry_iterate` 的实测，不是估算。

## 零、东西都在哪

工程已放在源库根目录下，`sources.py` 会自动识别源数据位置（不再写死盘符）：

```
E:\chinese_poetry_iterate\          ← 源库（2247 个 JSON，345 MB）+ PyCharm 项目根
├── .idea\runConfigurations\       ← PyCharm 运行配置，打开即有 6 个入口（make_runconfigs.py 生成）
└── poetry-pipeline\               ← 本工程（代码 + 产物）
    ├── dist\                      ← v3 产物：poetry.db / bundle / poetry-strains.db
    ├── .venv\                     ← 专用解释器（装了依赖；app.py 需要 tkinter）
    ├── requirements.txt
    ├── schema.py sources.py build.py query.py store.py app.py
    ├── external\                  ← 外接数据（歌赋 / 清代诗词 / 毛选模板）
    └── examples\                  ← 各平台接口例子
```

### 在 PyCharm 里用（三步）

1. **打开项目**：`File → Open` 选 **`E:\chinese_poetry_iterate`**（不是 poetry-pipeline 子目录——
   运行配置里的路径按这个根目录写的）。
   建议把数据目录标记为排除，否则 PyCharm 会给 2247 个 JSON 建索引、明显变卡：
   右键 `全唐诗` / `strains` / `rank` / `御定全唐詩` 等 → `Mark Directory as → Excluded`。
2. **选解释器**：`Settings → Project → Python Interpreter → Add → Existing environment`
   指向 `E:\chinese_poetry_iterate\poetry-pipeline\.venv\Scripts\python.exe`。
   *必须用它*——系统或别的 venv 里可能没有 tkinter，那样 `app.py` 起不来。

   选错的典型症状是 `ModuleNotFoundError: No module named 'zhconv'`（或 `zstandard`）：
   `zhconv` / `zstandard` 只装在本工程的 `.venv` 里，全局或别的工程的 venv 里没有。
   注意这条和运行配置是两回事——§零 那 6 个入口把解释器**写死在配置里**，即使项目解释器
   选错了，点它们也照样能跑；错的只会是右键 `Run` 自动生成的临时配置。
3. **跑**：右上角配置下拉里已经有 6 个现成入口（构建全量 / 校验 / 查询基准 / 桌面窗口 /
   API 服务 / 冒烟构建），直接点绿色三角即可。它们由 `poetry-pipeline/make_runconfigs.py`
   生成，统一指向 `$PROJECT_DIR$/poetry-pipeline/.venv/Scripts/python.exe`；改了脚本重跑即可，
   但**别改生成目录**——必须落在项目根的 `.idea`（见该脚本 docstring）。

### 源数据位置怎么定的

`sources.ROOT` 按此顺序探测，**整个工程可以跟着源库一起搬到任何盘，不用改代码**：

1. 环境变量 `POETRY_SRC`（最优先，临时换个源库时用）；
2. 工程目录的**上一级**（现在的摆法：工程在源库根下）；
3. 工程目录本身；
4. 兜底 `E:\chinese_poetry_iterate`。

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
uid       string  uuid5 内容派生（v3.1 起），形如 4a3c83d8-a6f2-46f7-852b-…-…；外部数据的关联键
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

### 3.1.1 `uid`：给外部数据用的稳定关联键（v3.1）

`id` 的低 24 位是**序号**，只要 dedup 丢/留一条、或集合顺序一变，序号就整体位移，
id 跟着全变。挂在主库外面的数据（extras 的译文/注释/赏析/配图）若按 id 关联，
**一重建就整体错位**——实测一次重建就会让 1.6 万条赏析全部挂错。

所以 v3.1 给 `poems` 加了 `uid` 列（唯一索引）：

```python
uid = uuid5(POEM_NS, "\x01".join([dynasty, author, title, body]))   # schema.make_uid
```

- **由内容派生、确定性哈希**，不是随机数：重建 N 次同一首诗得到同一个 uid，
  对「重建 / 重排序 / 增删别的诗」完全免疫。
- 字段组合是实测选的（345,358 首全量）：`朝代+作者+标题+正文` 冲突 **0**；
  去掉作者虽能换来「改作者不改 uid」，但会撞号 468 组 / 1094 行，不可用。
- **代价**：uid 是内容哈希，改动这一首自身的作者/标题/正文，uid 就会变。
  改完内容跑一次 `python remap_extras_uid.py --repair` 把失效的 uid 按内容重新指回即可
  （实测 150 条全部找回：100 条按「标题+正文」、49 条按标题、1 条按正文）。

**约定：外部数据一律用 `uid` 关联，不要用 `id`。**

| 场景 | 用什么 |
|---|---|
| 库内检索、范围过滤、分片 | `id`（int32，高位编码朝代/体裁） |
| extras / 外部引用 / 跨重建保持不变 | `uid` |

相关工具：
```
python remap_extras_uid.py --dry-run    # 把 extras.jsonl 的数字 id 换成 uid（演练）
python remap_extras_uid.py              # 真换（自动备份）
python remap_extras_uid.py --verify     # 校验 uid 命中率
python remap_extras_uid.py --repair     # 改过作者/标题后，按内容把失效 uid 指回新诗
python remap_extras_uid.py --dedupe     # 合并落在同一 uid 上的重复记录
```

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
- **扩展资料（译文 / 注释 / 作品简介 / 创作背景 / 赏析 / 配图）**：点「扩展资料」或右键同名菜单，
  弹出六合一编辑窗。这些内容存在**独立目录** `dist/extras/`（`extras.jsonl` + `images/`），
  图片本体下载到本地而 JSONL 里只存地址——详见 §5.5。
  - 详情面板按「译文 → 注释 → 作品简介 → 创作背景 → 赏析 → 配图」顺序展示已有的部分（没有的段落不显示）；
  - 配图缺失会标 `← 文件缺失`，非法地址会标 `← 非法地址`；
  - **清空某栏再保存 = 删除该字段**（底层合并语义是"非空才覆盖"，不放这个出口用户就没法删内容）；
  - 配图框每行一个地址，http 开头会自动下载进 `images/` 并转成相对地址；
  - 底部显示「资料来源：来源名 / 许可」，CC BY 资源的出处可追溯。

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

### 5.5 扩展属性层 extras.py（译文 / 注释 / 简介 / 背景 / 赏析 / 配图）

主库只存「原文 + 最小元数据」。下面这六项是**稀疏、大块、持续累积**的补充内容，
走一套完全独立的存储：`extras.py` + `dist/extras/` 目录。

| 字段 | 含义 | 存储短键 |
|---|---|---|
| 译文 | 现代汉语今译 | `tr` |
| 注释 | 字词疏解 | `nt` |
| 作品简介 | 整体题旨 | `in` |
| 创作背景 | 写作年代 / 际遇 | `bg` |
| 赏析 | 艺术赏析 | `ap` |
| 配图 | **图片相对地址数组**，不含二进制 | `im` |

**为什么又是独立 sidecar，不在 poetry.db 里加一张表：**

1. **生命周期不同**——主库是从源码重建出来的（`build.py` 跑一次全量覆盖），
   而这些内容是外部抓取 + 人工订正的累积成果。独立目录 `build.py` 永远不写，
   重跑构建完全免疫。
2. **极度稀疏**——34 万首里目前只有几百首有赏析。塞进 `poems` 主表等于让
   每行都背上 6 个空字段。
3. **便于分发**——整个 `dist/extras/` 可直接挂 CDN，消费方不必理解 sqlite
   结构，也不用为了读两条赏析 ATTACH 一个 110 MB 的库。

**目录结构与消费方式：**

```
dist/extras/
├── manifest.json     元信息：条数、字段表、来源登记、许可、图片统计
├── extras.jsonl      每行一首（JSON Lines，稀疏字段）
├── _cache/           外部原始文件的下载缓存（27.8 MB，避免重复下载）
└── images/           图片本体，文件名 = URL 的 sha1 前 16 位
```

JSONL 里**只登记相对地址**，图片本体在 `images/` 目录：

```json
{"i":536879037,
 "ap":"此诗以明月起兴…",
 "im":["images/b0bd265c9ac10d7f.jpg","images/6cfd5104bdfb1fd0.jpg"],
 "src":"shuge", "lic":"CC BY 4.0",
 "url":"https://www.shuge.org/wp-content/uploads/…/zi_jing_bian00.jpg"}
```

这样整个目录能整体搬走 / 同步到 CDN，地址依然有效；同一个 URL 只下一份
（按 sha1 去重，重复跑脚本不会累积副本）。

**为什么内部用 JSONL 而不是一个大 JSON：**全量 `json.dump` 要把几十万条字符串
同时在内存里拼好；JSONL 支持按行 seek、流式追加。读取侧先扫一遍建立
`{poem_id: 文件偏移}` 索引（只有 `{int: int}`，很轻），真正取用再 seek 一行读出，
不需要把全文驻留内存。

**数据来源与许可**（只有明确授权 / 公版 / 自生成三类可用——诗词本身是公有领域，
但他人的译文、注释、赏析属于现代再创作，受著作权保护）：

| 来源 | 许可 | 能补什么 | 实测 |
|---|---|---|---|
| `open-chinese/poetry-collection` | MIT | **仅赏析** | 4 个分段 27.8 MB，`appreciation.summary` 填充率 100%，挂接率 85% |
| 书格 shuge.org | CC BY 4.0 | 配图 | 可直接下载，走 `wp-content/uploads/YYYY/MM/…` |
| 人工录入 / 大模型生成 | user / AI-generated | 全部字段 | GUI「扩展资料」窗口直接写 |

> ⚠️ poetry-collection 的 `introduction` / `translation` / `notes` 字段实测**全部为 None**
> （字段是预留的，没有填），所以它只能补赏析，补不了译文和注释。
> 译文 / 注释建议用本地大模型生成或人工录入。

```bash
python fetch_extras.py appreciation                      # 导入全部四个分段的赏析
python fetch_extras.py appreciation --segments 95-100    # 先小跑一段看效果
python fetch_extras.py mklist ./external/images.txt      # 生成图片清单模板
python fetch_extras.py images ./external/images.txt \
        --referer https://www.shuge.org/                 # 按清单下载配图
python fetch_extras.py status                            # 概况 + 署名清单
python extras.py get 536879037                           # 查单条
python extras.py export ./extras-all.json                # 导出成单个 JSON 数组
```

抓取脚本做了三件实测必需的事：**多源轮换**（GitHub raw 断连时换 jsDelivr 镜像）、
**断点续传**（15.5 MB 的分段在中美链路上实测会中途断连，每次从 0 重来根本下不完；
GitHub raw 支持 Range，返回 206）、**坏缓存自删**（半份 JSON 若不删掉，
下次会命中缓存然后永远卡在同一个解析错误上）。

> CC BY 类来源要求署名。`extras.py attribution` 会输出分发时必须带上的清单，
> 内容也存在 `manifest.json` 的 `sources[].attribution` 里。

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
├── extras.py       扩展属性层：译文/注释/简介/背景/赏析/配图
│                   dist/extras/ 独立 sidecar（JSONL + images/），重建主库不受影响
├── fetch_extras.py 外部数据抓取：多源轮换 + 断点续传 -> 落入 extras（唯一联网的地方）
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

---

## 十四、源库繁转简 —— `simplify_json.py`

需求：源库 JSON 里的繁体字改成简体，**并且保持原来的格式**。
现在源库（`sources.py` 登记的全部集合 + `rank` + `strains` + `dist/extras`）已就地转换完成，
`--retry-verify` 复查「0 处残留」。

```bash
python simplify_json.py                      # 体检，不落盘
python simplify_json.py --apply              # 落盘（自动备份被改的文件到 backup/zhconv-<时间戳>）
python simplify_json.py --retry-verify       # 复查是否还有繁体
python simplify_json.py --apply --include-optional   # 连「御定全唐詩」可选包一起转
```

### 为什么不是读出来 `json.dump` 回去

这批文件至少 5 种互不相干的写法：4 空格缩进 LF、2 空格缩进 LF、4 空格 CRLF、
冒号后不留空格、行尾多一个空格……后几种 `json.dumps` 根本复现不出来。
直接 dump 会让 diff 里混进成片的重排噪声，评审时看不出到底改了哪些字。
所以脚本对每个文件走**三级降级**，每一级落盘前都要过校验：

| 级别 | 做法 | 适用 |
|---|---|---|
| 1 `json` | 先验证「原数据用某套 dumps 参数重序列化 == 原文字节」，再用同一套参数 dump 简体版 | 约 98% 的文件：字节级对齐，diff 只有汉字变 |
| 2 `text` | 整段文本跑 zhconv（词组级映射也覆盖到） | 非标准 dumps 形状的文件 |
| 3 `char` | 逐字符替换，只改汉字 | 前两级都不可用时 |

校验三条：能解析 / 等于目标简体版本 / **结构指纹**（类型+键+非字符串标量）与原文一致。
任一条不过就跳过该文件并报出来——宁可不转，也不半转。

实测：1021 个文件中 704 个有改动，2,250,777 个字符串字段，0 失败；
抽查两个文件看 `git diff --numstat`，增删行数完全相等（5674/5674、6543/6543），即纯 1:1 替换。

> **坑**：zhconv 单次转换**不幂等**，典型链路 `餘 → 馀 → 余`，一次 `convert()` 只走一级。
> 第一轮全量跑完仍有 168 个文件 / 665 个字段残留繁体，第二遍才收敛。
> 已在 `conv()` 里循环到不动点（最多 6 跳），不用再记着跑两遍。

## 十五、交叉核对 —— `crosscheck.py`

拿 **`https://www.chagushici.com/`** 当参照（robots.txt 全放行；书站点 Nginx 层有浏览器校验，
只认 Cookie `safe_token=ok`，脚本自带），做两件事：

1. **核对标题/作者/正文**，`apply` 子命令按可选规则写回源 JSON（默认只补缺作者）
2. **把译文/注释/作品简介/创作背景/图片按 poem_id 挂进 extras 层**（图片只存内部相对地址）

```
python crosscheck.py crawl              # 1) 抓列表页 → xcheck/html/（13247 页，20 首/页）
python crosscheck.py index              # 2) 解析 → xcheck/ref.db（SQLite）
python crosscheck.py check              # 3) 与源库比对 → xcheck/diff-<时间戳>.csv
python crosscheck.py apply --dry-run    # 4) 把结论写回源 JSON（先演练）
python crosscheck.py run --end 25 --limit 300        # 端到端小样本
python crosscheck.py extras --limit 500              # 详情 → extras 层
```

第 3 步只出报告，第 4 步才动源文件。**两者之间刻意留了人工判断的空间**：
报告 CSV 可以直接拿 Excel 打开，删掉不同意的行，`apply` 只按 CSV 里的行改。

`check` 的输出按判定分类，明细 CSV 只写不一致的行（加 `--all-rows` 连一致的也写）：

判定值按"值不值得看"分成三档。前两类默认写进 CSV，第三类只能加 `--all-rows` 才出现：

**值得看**

| 判定 | 含义 |
|---|---|
| 不一致:标题 / 作者 / 正文 | (作者,标题) 都对上了，某个字段却不同，附相似度。多半是异文或分段差异 |
| 正文一致作者不同 | **最强的一条**：正文逐字相同，作者却对不上——很可能是源库挂错了人 |
| 正文近似作者不同 | 同上，但正文相似度 ≥ 0.9（异文/题注差异），需人工过一眼 |
| 源库缺作者 | 源库作者为空或写「佚名/不详」，可以拿站内的作者补上 |

**同一人 / 同名 ≠ 同作**（也会进 CSV，但基本是噪声）

| 判定 | 含义 |
|---|---|
| 作者写法差异 | 归一后是同一人（庙号 `太宗皇帝`→`李世民`、字号 `苏东坡`→`苏轼`、异写 `朱庆余`→`朱庆馀`） |
| 同名异作 | 标题撞车而已，《杂诗》这种全宋重了几十次，不是源库的错 |
| 未收录 | 参照站没有同名作品。**不等于源库错了**——站内本身就不全，也可能还没抓完 |

> **判定逻辑的关键一条**：判断"是不是同一首"要靠**正文**，不能靠标题。
> 早期版本只比标题就判出 47,294 条「疑似作者不同」，实测几乎全是标题撞车；
> 改成先比正文、再判作者之后，真正可疑的只剩 275 条。
> 作者比较前会过 `canon_author()`（转简体 + `ALIAS` 别名表），
> 别名表刻意收得短——收错一条就把两个真人并成一个，比漏报更糟。

### `apply`：写回源 JSON

```
python crosscheck.py apply --dry-run -v                 # 演练，逐条打印改动
python crosscheck.py apply                              # 默认只启用 missing 规则
python crosscheck.py apply --rules missing,author       # 额外启用写法归一
python crosscheck.py apply --csv xcheck/diff-xxx.csv    # 指定报告（默认用最新一份）
```

CSV 里的 **文件内序号** 就是记录在所属 JSON 文件里的下标，`apply` 靠它精确定位。

规则默认只开一条，其余必须显式写进 `--rules` 才生效：

| 规则 | 默认 | 改什么 | 风险 |
|---|---|---|---|
| `missing` | **开** | 源库作者为空/佚名/不详，参照站给的是**真名** → 补上 | 低。参照站作者是「佚名」「诗经」这类占位/书名时会被拦掉 |
| `author` | 关 | 归一后是同一人，只是写法不同 → 统一成参照站写法 | 中。「太宗皇帝 → 李世民」两边都对，选哪个取决于用途，不该由脚本决定。内置两道闸门：只接受「一个是另一个的前/后缀」的增删（如 `释贯休`→`贯休`、`尹鹗 六首`→`尹鹗`），且去掉的部分里不能出现亲属称谓（否则会把 `唐晅妻张氏` 改成 `唐晅`——作者从妻子变成丈夫） |
| `diff-author` | 关 | 正文几乎一致（≥0.9）但作者对不上 → 改成参照站作者 | **高**。参照站自己也会挂错人 |
| `title` | 关 | 标题不同且形近度 ≥ `--min-title-sim`（默认 0.9）→ 改标题 | **高**。标题差异常常是源库的刻意取舍 |

### `reconcile`：按报告改回 + 消除已消差异（瘦身报告）

`apply` 写完就结束；`reconcile` 在 `apply` 的基础上多一步**复核消除**——把报告里能安全修的差异写回源 JSON 后，
逐条重新比对差异点：凡是写完后和参照站一致的点，就把这条差异从报告里**消掉**，
产出两份报告：

- `reconciled-<时间戳>.csv`：只剩**还没消掉**的差异（疑似不同人、正文异文、低相似标题、缺 title 键等），留给人工。
- `eliminated-<时间戳>.csv`：本次**已消除**的差异留存，便于追溯。

```
python crosscheck.py reconcile --dry-run -v     # 演练：看会改哪些、能消掉多少
python crosscheck.py reconcile                  # 真写源 JSON（字节级最小改写，备份在 xcheck/patch-backup/）
python crosscheck.py reconcile --csv xcheck/diff-xxx.csv   # 指定报告
python crosscheck.py reconcile --min-title-sim 0.85        # 标题形近阈值（默认 0.85）
```

安全闸门与 `apply` 一致（同一人判定 / 亲属称谓守卫 / 占位符拦截 / 标题低相似留人工）；
正文差异**不会**自动改，所以带正文的行即使作者、标题都修好，也仍留在 `reconciled-*.csv` 里标「正文待人工」。
这样能反复跑：`reconcile` 出瘦身报告 → 人工处理剩余 → 再 `reconcile`，直到报告里只剩必须人判断的。

### `diff`：重新解析 JSON，明确标出差异位置（作者 / 标题 / 正文）

`check` 的「判定」列把 `作者+正文` 之类混在一起写，不够直观。`diff` 在同样「重新解析源库 JSON + 对照 ref.db」的基础上，
把差异**拆成三列独立判定**（作者一致 / 标题一致 / 正文一致），并额外给出：

- **差异类型**列：直接写 `作者` / `标题` / `正文` / `作者+正文` / `标题+正文` / `作者+标题+正文`（未命中分支沿用原判定词如 `未收录`/`同名异作`/`源库缺作者`/`作者写法差异`/`正文一致作者不同`）。
- **作者差异说明**：`源：X → 网站：Y`，一眼看出作者差在哪。
- **正文差异说明**：`源首段：… → 网站首段：…` + 正文相似度，看出正文差在哪。
- 末尾「**差异位置分布**」汇总：分别统计只有作者差异、只有正文差异、两者兼有各多少条。

```
python crosscheck.py diff                  # 全量：逐首重新解析源 JSON 对照参照库，产出 diff2-<时间戳>.csv
python crosscheck.py diff --limit 3000     # 小样本先看差异位置分布
python crosscheck.py diff --all-rows       # 连一致（判定=一致）的行也写进 CSV
```

产物 `xcheck/diff2-<时间戳>.csv` 列：集合/文件/文件内序号/原标题/源作者/源正文(首段)/网站标题/网站作者/网站正文(首段)/网站链接/作者一致/标题一致/正文一致/差异类型/作者差异说明/正文差异说明/标题相似度/正文相似度/备注。

### `reconcile_gui.py`：差异校对图形界面（打包成 `reconcile_gui.exe`）

不想敲命令就用界面版：打开 diff 报告 → 表格分页看每条差异 → 点开一条在右侧把
「作者 / 标题」设为 **保持原值 / 采用网站值 / 自定义** → 点「保存修改」写回源 JSON
（字节级最小改写 + 备份在 `xcheck/patch-backup/`），并导出 `reconciled-*.csv`（剩未消差异）
和 `eliminated-*.csv`（已消除留存）。

```
python reconcile_gui.py            # 直接跑（需 tkinter）
# 或打包成单文件 exe（用带 tkinter 的 Python，如系统 3.14）：
pyinstaller --onefile --windowed --name reconcile_gui reconcile_gui.py
```

- 启动自动探测工程目录（同级/上级含 `sources.py` 的目录），找不到会弹窗让你选，并记住到 `.reconcile_root.txt`。
- 借 `sources.py` 的 `COLLECTIONS / glob_shards / ROOT / load_json / get_body / pick_title`
  做路径解析与正文取值；其余逻辑（clean / 相似度 / 作者归一 / 字节级改写 / 详情页抓取解析）
  都在本文件内，不依赖第三方库（zhconv 可选，没有就跳过繁转简）。
- 顶部「演练(不写盘)」勾选后只校验、不落盘；「本页全用网站值 / 本页全跳过」可批量操作。

**右侧比对区**：选中一条时，上下两块是**同一版式**——`标题：` / `作者：` 打头，接着正文：

- 上格 = 源 JSON **解析后**的标题 / 作者 / 正文（`load_json` + `get_body` + `pick_title`，
  与当初算相似度用的是同一段文本）。想看原始 JSON（字段名、正文以外的字段）点「完整 JSON」。
- 下格 = 网上（chagushici.com 详情页）的标题 / 作者 / 正文。
- 两边标题都先剥掉**外层**书名号再显示、再比：详情页 `h1.maintitle` 里是 `《饮马长城窟行》`，
  本地 JSON 里是裸的 `饮马长城窟行`，不剥就永远看着/判着不一样。标题内部的《》不动
  （如「见道边死人（一作…《统签》并入…）」那种校勘夹注）。

摘要行给出 `标题一致/不一致`、`作者一致/不一致`（作者按 `canon_author` 归一）、
`网上侧`取自哪一档、两边句数/字数、正文相似度（按**全文**算）。

`标题/作者` 的对比**一定会出**，不依赖详情页抓没抓到——网上侧按下面的优先级取，
取到哪一档就在摘要里标哪一档：详情页/`ref.db` 有值就用它（`网上侧：详情页` / `网上侧：列表页`），
没有就退回报告行自带的 `网站标题`/`网站作者`（`网上侧：报告列（列表页快照）`）。
详情页到了会自动升级成详情页的值。

补缺是**逐字段**的：`ref.db` 里有些行作者本来就是空的，详情页 `h1` 也可能只有标题
（作者缺失），这时缺的那个字段单独从报告列补，标成 `网上侧：详情页／报告列补缺`。
不这么做的话作者一侧空着、`作者一致/不一致` 整条不出现，看着又像「作者没比对」。

- 来源优先级：`xcheck/html/shi-<sid>.html` 缓存 → 联网抓（抓完落进同一份缓存，
  与 `crosscheck.py extras` 共用，也共用同一套 UA/Cookie/有效性判据）→ 抓不到才退到
  `ref.db` 的列表页预览。只有前两种来源才显示相似度。
- 三档全落空时，下格会写明原因（`没取到网上内容——详情页抓不到（本地无缓存、ref.db 也没这条）`），
  不再是一句「原因未知」。
- 抓取是**选中就自动**在后台单线程做的，新请求顶掉旧请求，连点十几条不会并发打站；
  「联网」勾选框可全局关掉，关掉后只读缓存 + `ref.db`。
- 报告里的「正文相似度」与这里的数**不一致是正常的**：报告那列是拿 `ref.db` 的
  **列表页预览**算的（列表页每条只给 4 句），长诗会被截断，所以那个数系统性偏低。
  已知样本 `shi-306`：报告 0.557，实际全文只差一个异体字（0.99）。面板里的
  「源文件已改过」提示同理——报告是历史快照，源文件可能已被上一轮写回改过。

**出错了不会静默**。Tk 回调里的异常默认只往 stderr 打，而打包成 `--windowed` 的 exe 后
stderr 是空的——界面会停在旧内容上、日志里一个字都没有，看着就像「选了新数据却没刷新」。
所以 `report_callback_exception` 挂了钩子：异常一律追加到 `reconcile_gui.log`，前 3 次还会
弹窗提示（再往上只落盘，避免某个反复触发的回调把弹窗刷成风暴）。界面上的
「出错了：…」状态提示同理。

**正文永远不自动改**。版本异文、分段差异、`⌂` 这类校勘记号，机器判断不了。

### 写回怎么保证不动格式

不重新序列化整个文件——`detect_format()` 一旦猜不中原文件的缩进/分隔符风格，
几千行的文件会整体变形，diff 没法看，git blame 也会把整份历史算到改动头上。
实际做法是先用 `top_spans()` 扫出每条记录在原文里的字节区间，
只在那个区间里替换目标字段的值，**其余字节一个不动**，
再挂三层校验（写入值是否相符、未改记录是否原样、记录数是否不变），
任一条不过就直接拒绝写入。

实测：每条改动在 git 里就是一行增删。备份落在 `xcheck/patch-backup/<时间戳>/`。

### `extras` 的字段映射

| 详情页区块 | extras 短键 | 长键 |
|---|---|---|
| 译文 / 翻译 | `tr` | translation |
| 注释 / 释文 / 解释 | `nt` | notes |
| 创作背景 | `bg` | background |
| 鉴赏 / 赏析 / **诗意** / 品鉴 | `ap` | appreciation |
| 作品简介 | `in` | intro |
| 页面图片（作者头像等） | `im` | images，**只存 `images/<sha1>.jpg` 这样的内部相对地址** |

> **站点 2026-09 改版过一次**，资料容器 class 从 `ziliao` 变成 `shici-ziliao`，结构也从
> 「`<h2>` 定类型 + `<strong>` 分段」变成「单个 `<p>` 内用 `标签：` + `<br>` 分段」。
> `parse_detail()` 现在**两种模板都认**，标签走包含匹配（`中文译文`／`诗词的中文译文如下`／
> `诗意和赏析`／`《病柏》诗意与赏析`…都是同一个标签的不同写法）。
> 注意点：标签长度卡在 **≤12 字**——页面开头的引导语
> 「以下是对这首诗词的中文译文、诗意和赏析：」也含「译文」「诗意」，不限长度会被整句当成译文写进去。

行为约定：

- **已有字段不覆盖**：`ap` 里已经躺着的 MIT 赏析不会被后来的来源顶掉，
  确认要替换就加 `--overwrite`；跳过了多少字段会在汇总里报出来。
- **断点续跑 `--resume`**：跳过本来源已写过的条目，进度记在 `xcheck/extras-done.txt`。
  判据**只看写入结果，不看 `xcheck/html` 缓存**——页面抓下来 ≠ 解析出内容，
  按缓存跳过会把「抓了但解析失败」的那批永久跳掉。
  缓存依然有用：命中时是本地读文件、零网络开销，重解析几乎不花时间。
  ```bash
  python crosscheck.py extras --resume --workers 6 --sleep 0.2
  ```
- 挂接按 `(标题,作者) → (标题,首句) → 仅标题` 三级退化，命中层级会在开头打印。
  「仅标题」占比越高，挂错的风险越大，需要时改 `poem_index()` 去掉 `by_t` 兜底。
- 图片下载走 `extras.save_image()`：文件名 = URL 的 sha1 前 16 位，同一 URL 只存一份。

> **许可**：该站页面（含页脚）没有出现任何 CC / 公有领域条款，译文注释鉴赏都是现代白话作品，
> 默认受著作权保护。已在 `extras.KNOWN_SOURCES["chagushici"]` 登记为**未声明开放许可**，
> 会进 `manifest.json` 和 `attribution_lines()` 的署名清单。**自用/研究可以，
> 对外分发前先取得授权**，别把这条从清单里删掉当没看见。

### `llm_enrich.py`：LLM 补齐缺失字段

站点侧拿不到的东西（**作品简介 `in` 一条都没有**，注释 `nt` 仅 2031 条、创作背景 `bg` 仅 1375 条）
只能靠模型生成。`llm_enrich.py` 干这件事，**零新增依赖**（只用 urllib，跟 `crosscheck.py` 一致），
走 OpenAI 兼容协议，换模型只要改 `--base` / `--model`。

```bash
# 1) 给 Key。走环境变量而不是命令行参数——命令行会被记进 shell history
set DEEPSEEK_API_KEY=sk-xxxxxx          # Linux: export DEEPSEEK_API_KEY=...

# 2) 先跑 1 条看质量：不写盘，把请求、原始回复、解析结果都打出来
python llm_enrich.py --print-sample

# 3) 试点：在全库候选里**随机**抽 500 首（不要用 --limit，见下）
python llm_enrich.py --sample 500 --workers 8

# 4) 看刚才那批生成得怎么样
python review_llm.py -n 5

# 5) 全量补缺之前，先演练看量级和预估花费
python llm_enrich.py --dry-run
```

几个刻意的取舍：

| 设计 | 原因 |
|---|---|
| **用 `--sample` 而不是 `--limit` 做试点** | `--limit` 是按库顺序取前 N 首，而源库按选本排列，前 500 首会全撞在同一批作者上，据此判断效果好坏会误判。`--sample` 打散抽样，**且会把清单存成 `xcheck/llm-plan-<N>-<seed>-<fields>.json`** |
| **抽样清单必须落盘** | 候选集合随写入变化（补上 `in` 之后这 500 首就"不缺"了），同一颗 seed 第二天抽不到同一批。实测踩过：换 LLM 后 `--resume` 跳过 0 首，等于把另一批 500 首重新生成了一遍。`--resume` 现在会先复用清单 |
| **只生成缺失的字段** | 已有译文的 8.4 万首没必要重生成一遍，还白白花钱且可能不如原文准 |
| `--max-body 2000` | 主库混了非诗词条目（《古文观止》14.2 万字、《唐诗三百首》《幼学琼林》…），给它们生成简介是浪费。阈值留到 2000 是为了不误伤《孔雀东南飞》《离骚》这类真长诗 |
| **默认跳过蒙学/选本** | 《百家姓》《三字经》《千字文》正文只有几百字，绕得过长度闸门，但叫模型给《百家姓》写译文它只能把姓氏复述一遍（实测 861 字纯废话）。按标题黑名单跳过，`--include-nonpoem` 可放行。**只跳过生成，主库数据不动** |
| prompt 里硬性要求「拿不准就留空、禁止编造背景」 | 空值不写入，且单独计 `空值 N 首`，这个数偏高说明 prompt 太保守或素材不适合生成 |
| 四级 JSON 容错 | 代码块 → 多对象合并 → 松散抠值（救被 `max_tokens` 截断的输出）→ 才判失败。试点时 500 首里 10 首栽在截断上，加了这层之后同类问题基本消失 |
| 写入串行 / 抓取并发 | `ExtrasStore` 内部有 LRU + dirty 集合，多线程写入会把未落盘的记录挤掉（crosscheck 里踩过） |
| 来源登记 `llm` | `lic` 写成 `AI-generated (deepseek-chat)`，保证 manifest / attribution 里能看出是 AI 生成、未经人工校勘 |

**实测（deepseek-chat，500 首随机抽样，4 字段全补）**：

- 通过率：第一批 490/500（10 首失败全是被 `max_tokens` 截断导致 JSON 不完整），
  修好容错解析器后重跑的同批 500/500 全过
- 花费：500 首 ¥1.66（输入 16.6 万 + 输出 16.6 万 tokens），折合 **¥0.0033/首**
- 速度：8 并发约 190 秒跑完 500 首 → 全量 34.5 万首约 **36 小时**
- 产出长度中位数：译文 87 字、注释 149 字、简介 68 字、背景 81 字（约 2/3 的诗给了背景，
  其余是模型判断「无可考背景」留空，符合要求）
- 抽检用 `review_llm.py`，-n 5 随机打印原诗 + 生成内容并排对照

**用量参考**（用 `--dry-run` 可实时估算）：当前缺 `in` 的约 34.5 万首（全库口径），
按上面的单价全补约 ¥1150；只补已有 extras 记录的 9.6 万首约 ¥320。
耗时才是大头——建议分批 `--sample` 跑，每批一张清单，坏了能精确补跑。

## 十六、第三方参照：百度百科 —— `baike_crosscheck.py`

`crosscheck.py check` 走的是 chagushici（查古诗词）：7 万条差异要逐条回站核对，
一次全量十几个小时，而且参照站一改版（2026-09-29 那次 `ziliao` → `shici-ziliao`）
解析器就全废。所以补一个**第三方参照**做交叉验证：百度百科。

```bash
# 1) 先探命中率：这批差异有多少能被百科覆盖？（强烈建议先跑）
python baike_crosscheck.py probe -n 200 --show 6

# 2) 按百科订正（默认演练，--apply 才写盘）
python baike_crosscheck.py apply --dry-run
python baike_crosscheck.py apply --apply

# 3) 出问题一键回滚（报告里存着完整原文，不用去 git 里捞）
python baike_crosscheck.py rollback --csv xcheck/baike-<时间戳>.csv --apply
```

### 抓取方式

| 端点 | 用途 |
|---|---|
| `baike.baidu.com/api/openapi/BaikeLemmaCardApi` | 轻量卡片（几 KB）：判断条目是否存在，拿作者/朝代/体裁 |
| `wapbaike.baidu.com/item/…` | 移动端页面。PC 页对脚本 403，wap 端可访问，且内嵌 `__NEXT_DATA__`，正文是结构化的 |

### 三条守卫（缺一不可）

1. **作者必须校验**：诗词同名极其严重（《春望》可以是杜甫也可以是别的），
   不校验作者就把另一首同名诗的正文写回去 = 数据投毒。两边都过 `canon_author`
   归一（庙号改本名、去朝代前缀）再比。
2. **只认「作品原文」小节**：条目里还有注释译文、赏析，把它们当正文写回就毁了。
   抠不出该小节就判「不能采纳」，**绝不退化成"抓第一段"糊弄过去**。
   只取第一个二级小节——条目常并列给出「明代版本/宋代版本」，全收会自相矛盾。
3. **相似度门槛**：`--min-sim 0.55` 以下视为不同作品不采纳，
   `--variant-sim 0.90` 以上且不等才按通行本订异文。
   最优先接受的是「源正文是百科正文的前缀」——源库大量长诗被截成首段，
   这类补齐收益最大也最安全。

### 踩过的坑

- **内链词被丢**：百科把诗句里的典故做成内链（`{"tag":"innerlink","text":"镂衢"}`），
  只收 `tag=="text"` 的节点会掉字 ——《奉和九月九日应制》抠出来变成
  "玉砌分雕戟，金沟转。"（掉了"镂衢"）。**写回后抽查才发现的**，靠报告一键回滚。
  教训：外部数据的解析器改完必须抽查 2~3 条真实写入结果，不能只看统计数字。
- **报告正文不能截断**：曾经为了文件小把正文截到 120 字，回滚时才发现没法用，
  只能靠"源正文恰好 12 字"的运气。现在报告存完整原文。
- **备份文件名要用相对路径**：`patch_file` 原先按 basename 备份，
  不同集合下的同名文件互相覆盖 —— 一次改 30 个文件只剩 1 份备份。已修。
- **条目说明被当成诗句写进正文（首轮全量，最隐蔽）**：百科「作品原文」小节
  常常在诗句后面接着写 "该诗收录于《全唐诗》卷三"、"（五言律诗，押尤韵。
  出自《全唐诗》卷1_53。）"、"注：诗中'雕宫'一作'彤宫'…"、"拼音：…"。
  首轮 9024 条里 **1198 条（13.3%）**混进了这类文字。
  修法见 `strip_notes()`：**诗句一定在前、说明一定在后** ⇒ 逐行找第一个说明
  标记，命中就砍掉该行余下部分并停止。脚注编号`(1)`夹在句中，只删不当终止符。
- **清洗规则改了不要重跑 apply**：重跑要再抓 6 万个页面（3 小时）。报告里存着
  完整百科原文，用 `reclean` **在本地重算一遍即可**，零网络请求：
  ```bash
  python baike_crosscheck.py reclean --csv xcheck/baike-<时间戳>.csv --apply
  ```
  它有守门：清洗后必须仍是原正文的"超集"，且只许变短，否则跳过不写 ——
  宁可不动，也不能削掉诗句。

### 全量实测（2026-09-30，61,405 个查询键 / 70,772 行差异）

| 判定 | 条数 | 说明 |
|---|---:|---|
| 百科没有可提取的原文 | 28,784 | 条目存在但没有「作品原文」小节 |
| 作者不符，疑似同名异作 | 20,470 | 守卫拦下，未采纳 |
| **源被截断，按百科补齐** | **9,024** | **唯一写入的一类** |
| 百科无此条目 | 7,119 | — |
| 正文相似度低，疑似不同作品 | 5,063 | 守卫拦下 |
| 需人工判断（0.55~0.65） | 311 | 写进报告留给人工 |
| 异文，按百科通行本订正 | 1 | — |

耗时 2h59m（12 并发），**改写 9,025 处 / 286 个文件 / 0 失败**，
随后 `reclean` 又洗掉 1,389 条里的条目说明，最终 9,025 条全部干净、286 个 JSON 全部合法。

### 改完源 JSON 之后必须做的两件事

1. **重建成品库**：`python build.py --out ./dist`
2. **刷新 extras 的 uid**：`python remap_extras_uid.py --repair --dedupe`
   —— `uid` 是内容哈希（见 `schema.make_uid`），**正文一改 uid 就变**，
   不改的话这 9,025 首的译文/注释/赏析会集体挂不上。

### 合规

百度百科采用 CC BY-SA 3.0。本脚本**只做事实性订正**（补全被截断的正文、
统一异文），不搬赏析/注释；报告里带条目 URL 便于回溯署名。

## 十七、正文数组的格式保真 —— `patchjson.py` / `fixjson_body_format.py`

### 事故

源库的正文字段原生形态是**数组，一句一行**：

```json
"paragraphs": [
  "古径约城斜，锄荒可过车。",
  "直穿深筿去，不比绕村赊。"
],
```

而 `patch_file()` 当年只支持写字符串：`json.dumps(字符串)` 塞回去就把数组覆盖成了
**一整条内含 `\n` 的字符串**：

```json
"paragraphs": "古径约城斜，锄荒可过车。\n直穿深筿去，不比绕村赊。",
```

2026-09-30 百科订正那次，**286 个文件 / 9,026 条记录**被写成这样——排版全毁。

### 根因修复（已做）

`patchjson.patch_file` 现在**保持原字段类型**，新增三个函数：

| 函数 | 作用 |
|---|---|
| `literal_for(seg, idx, field, val, data)` | 看原值类型：原来是 list 就按 `\n` 拆回 list；原来才是 str 才原样写 |
| `render_list_like(old_text, items)` | 按原排版渲染数组——把「元素前缀 / 分隔符 / 元素后缀」三段空白**从旧文本里抠出来复用**，所以缩进、对齐、**行尾空格**都与原文一致，而不是猜一个 |
| `text_of(v)` | list 按行 join，用于跨类型比较 |

配套把两处校验从「对象相等」改成「文本内容相等」——否则写 list 会被自己的断言拦下：
写后取值校验、以及 `期望旧值` 的时间差断言。

> `crosscheck.py` 里那份同源副本已随 chagushici 参照站退役移除。
> **`patchjson.py` 现在是全工程唯一的写语料入口**（GUI、Web 编辑、命令行都走它）。

### 事后修复

```bash
python fixjson_body_format.py            # 演练
python fixjson_body_format.py --apply    # 落盘，逐文件备份到 xcheck/fixjson-backup/
```

判定规则（**实测过才敢这么写**）：抽样每个集合的 `git HEAD` 版本统计——
除 `youmengying`（幽梦影原生就是 str）外，**全部原生 list，
且 HEAD 里没有任何一个含 `\n` 的字符串**。所以：

```
body 是 str 且该集合不是 youmengying  ⇒  一定是我们写坏的
```

排版模板从**同文件里一条未被改动的同类记录**那里取，不猜缩进。
另有两道保护：文件里找不到任何原生数组（说明判定可能错了）就跳过该文件；
写盘前校验「记录数不变、改动项取值相符、未改动记录逐条完全相同」。

**还原后 uid 完全不变**：`sources.get_body` 对字符串形态返回 `[整串]`，
`"\n".join(["句1","句2"])` 与直接给 `"句1\n句2"` 结果相同，
所以 `make_uid` 算出来一模一样 —— 实测 345,339 个 uid **逐一比对零差异**，
extras 一条都不用重挂。
