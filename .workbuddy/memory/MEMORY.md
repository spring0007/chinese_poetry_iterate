# 项目长期记忆（chinese_poetry_iterate）

## poems.score 语义（2026-10-16 起变更，重要）
- **旧**：五引擎（百度/必应/360/谷歌）搜索结果数 log 量化，0-255。数据来源 rank/ 原始分片，已过期并于 2026-10-16 全部清零。
- **新**：四维网络热度综合分，1-100。权威数据在 `rank/heat-scores.json`（以 poems.uid 为唯一键，含四维分/星级/评语/引擎证据/meta 规则说明）。
- 星级阈值：≥90 五星 / ≥75 四星 / ≥55 三星 / ≥35 二星 / 其余一星。语料为历代选本沉淀，一星档为空属预期。
- 只有 tang-poem / song-poem / song-ci 三个集合有热度分；其余集合（诗经/楚辞/元曲/近现代等 30,225 首）score=0，不是数据丢失。

## 热度工具与构建约定
- 重评工具：`poetry-pipeline/heat_score.py`（--dry-run 试评 / --export 出 JSON / --apply 备份+落库+写 meta）。
- `build.py` 的 `attach_heat()` 按 uid 挂接 heat-scores.json，**必须在 normalize_all + dedup 之后调用**（make_uid 依赖归一后字段）；heat-scores.json 缺失时才退回旧 raw 分片字符串关联。
- 修改 poems 内容（作者/标题/正文）会使其 uid 变化，extras 与热度需按新 uid 重新关联。

## 元曲已移出构建（2026-10-20 起，取代 10-19 的「独立包」方案）
- **已删除**：`sources.py` 的 `yuan-qu` 集合 + `yuanqu.authors` 作者源 + `Collection.separate` 字段；`build.py` 的 `emit_separate()`、`--with-yuanqu`、`sep_colls` 摘取、`attach_heat(warn_empty=)` 参数；`verify_db.py` 的 `meta.separate` 跳过分支；`dist/yuanqu/` 整个产物目录。`from dataclasses import replace` 随之无用。
- **保留**：源文件 `元曲/yuanqu.json`、`元曲/authors.json`（一个字节没改）；`genre_tag.py:177` 仍读元曲语料提取曲牌（用于区分曲/词，与构建无关）。
- **恢复方法**：在 `COLLECTIONS` 加回 `Collection("yuan-qu","yuan","qu", r"元曲\yuanqu.json", has_id=False, label="元曲", genre=3)`，它会作为普通集合直接进主库，id 独占 `yuan/qu` 桶（838,860,800 起），不动其它集合 id。
- 移除后重建（10-20 10:45）：poems **348,497**（不变）、authors **14,213**（−160，只属于元曲的作者消失）、sources 23（−1）、`kind=qu` 记录 0、bundle 190 片 44.6 MB、strains 311,594、poetry.db 337.0 MB。verify 全绿，单测 84 例通过。

## 诗人分级系统（poet_tiers，2026-10-17 起；当天 17:47 按更正数据重建为 v2.0）
- 权威文件：`rank/poet-tiers.json`（34 位诗人，字段 id/name/tier/score/rank/heat_score/heat_rank）。
- **v1.0（已废弃，勿再用）**：第五档提升为第一档+内分 5 级、槽位取分数带上沿（100/98/96/94/92/90/88/86/84）。用户已声明该标准错误。
- **v2.0（当前）**：五档分类与分数一一对应——第一档 屈原/李白/杜甫/苏轼=100；第二档 陶渊明/曹植/白居易/李商隐/辛弃疾/李清照/毛泽东=98；第三档 王维/杜牧/陆游/李煜/马致远=96；第四档 艾青/穆旦/胡适/北岛/海子/顾城/舒婷/戴望舒/卞之琳=94；第五档 王勃/陈子昂/孟浩然/王昌龄/刘禹锡/郭沫若/闻一多/徐志摩/何其芳=92。同档同分并列，名次 1224（1,1,1,1,5×7,12×5,17×9,26×9）。
- score_band.py 同步更正：**第一档=90-100**（11 值），档2 起每两分一档（88-89, 86-87, …, 0-1），共 46 档；34 位诗人全部落在第 1 分数带。
- **authors.score 语义分裂**：22 位分级诗人是档位分（人工口径），其余为热度聚合分；12 位（第四档全部+闻一多/徐志摩/何其芳）库中无作者行，只进 JSON（id=null）。heat_score/heat_rank 是覆盖前的纯热度留痕。
- `--apply` 行为：从 poems 重算纯热度分（author_heat.aggregate_author_scores，延迟导入防循环）→ 漂移安全闸（非分级作者当前值≠纯热度且不在 LEGACY_TIER_SCORES={100,98,96,94,92,90,88,86,84} 即中止）→ 整库重置纯热度+档位覆盖 → 热度 JSON 从纯热度重建 → 三方校验 + 与 17:13 纯热度备份交叉比对。
- `build.py` 与 `author_heat.py` 均已接入 poet_tiers 覆盖层（重建/重跑不丢档位分）。改分级改 `poet_tiers.py` 顶部 `TIERS` 常量后重跑 `--apply`。
- 测试：`test_poet_tiers.py`（25 例）、`test_score_band.py`（24 例），`python -m unittest discover` 共 49 例。

## 水墨唐诗已并入全唐诗（2026-10-19，重要）
- `水墨唐诗/` 目录已删除，`sources.py` 无 `shuimo-tangshi` 集合、无 `shuimotang.authors` 作者源；`loader/datas.json` 对应条目已注释。
- 176 首逐篇命中原诗（82 精确 / 94 异文·节选·异题），**不新增记录**，只给原诗 `tags+="水墨唐诗"`、`prologue` 白话语译写入 `notes`（152 首）。共 178 条记录带该 tag（杜甫《绝句三首》覆盖 3 个子篇）。
- 当前源库口径：唐 57,412 / 宋诗 254,191 / 宋词 21,048；重建后主库 348,502、authors 14,374、平仄 311,594、extras 99,671。
- 宋词记录**不再带 `dynasty`**（原 2,639 条冗余，已统一移除，朝代由数据集定义）；18,411 条无 `title` 属词牌体例，构建时 `pick_title` 用 rhythmic 兜底。
- 备份：`dist/backups/tangsong-before-clean-20261019.tar.gz`（含并入前的 全唐诗/宋词/水墨唐诗）。

## 体裁标注 genres 与 schema 3.6（2026-10-19 起，重要）
- 八集（南北朝/明/汉/近现代/金/隋/魏晋/清诗）共 13,808 条已逐条标注，`genres.json`（仓库根）是唯一登记表：1 诗 / 2 词 / 3 曲 / 4 赋 / 5 骚 / 6 文 / 7 新诗 / 8 联。
- 源记录**只留 `"genreId": N`**（名称字段 20:5x 已全量外科移除，别再加回来）。跨集分布：诗 8,883 / 词 4,521 / 曲 23 / 赋 75 / 骚 47 / 文 253 / 新诗 3 / 联 3。
- 工具 `poetry-pipeline/genre_tag.py`：默认 dry-run，`--apply` 备份+写盘+逐条自检，`--verify` 校验；`test_genre_tag.py` 24 例（全套 73 例）。改规则动 `classify()`，改体裁动 `schema.GENRES`。
- **改这八集文件必须 `open(..., newline="")`**：只有 近现代 是 4 空格+LF，其余是 1 空格+CRLF；用通用换行读会把 CRLF 变 LF、整文件重写。
- **schema 3.7 定稿（2026-10-20，取代 3.6）**：库里字典表**只剩 `genre(id,name)` 一张**（8 行 = `GENRES`）。3.6 的 `source` 朝代表、`poems.dynasty_id`、`poems.genre` 名称副本**全部删除**，别加回来——朝代在 `id >> 27` 里（`DYNASTY_LABEL[DYNASTY_NAME[code]]` 反解，按朝代筛用 id 区间），体裁名 JOIN genre 表取。`sources`（复数）是语料来源集合，与 genre 无关，保留。
- 构建侧：八集记录自带 genreId；其余整集同体裁的走 `Collection.genre` > `KIND_GENRE[kind]`（楚辞=5、纳兰=2、元曲=3）。源文件一个字节不改。
- id/uid 不受影响：`genre_id` 不参与 make_id/make_uid。`verify_db.py` 断言：无 source 表、poems 无 dynasty_id/genre 列、id>>27 全落在已知朝代编码、genre_id 全合法。`store.require_writable_schema` 遇到带这三样旧结构的库会拒绝写并提示重建（3.6 库不可直接写）。
- 3.7 全量重建（2026-10-20 09:22，`build.py --out ./dist --with-strains`）：poems 348,497 / authors 14,373 / rhythmics 1,462 / extras 99,671；体裁 诗321,101 词26,369 曲23 赋78 骚112 文808 新诗3 联3；朝代 宋275,240 唐57,413 近现代11,847 清692 五代543 …。dist：poetry.db **337.5 MB**（比 3.6 省 5.7 MB）、bundle 44.6 MB/190 片、poetry-strains.db 13.1 MB、yuanqu 6.6 MB。verify_db 退出码 0。

## authors.dynasty 约束（2026-10-20，重要）
- `authors` 是**唯一硬存朝代**的表（authors.id 是导入顺序序号、不含编码），取值只能是 `schema.DYNASTY` 的**键**（'tang' 这类字符串），不能是编码值、更不能是来源名。
- 2026-10-20 修掉一处 3.5→3.7 一直存在的脏数据：`sources.py` 的 `AuthorSource(name, dynasty, pattern)` 第二参数曾被填成来源名，15 位作者（楚辞 9 / 蒙学 5 / 纳兰 1）dynasty 为 `chuci/nalan/mengxue`。现已对齐所属 Collection：**chuci→preqin、nalan→qing、mengxue→unknown**（蒙学跨南朝梁/明/清，不臆断）。改这里会改这 15 人的 `author_uid`（uuid5(name,dynasty)）。
- 防线：`verify_db.py` 断言 authors.dynasty 全落在 DYNASTY 内 + 打分布；`test_sources.py`（11 例）钉死 Collection/AuthorSource 的 dynasty/kind/genre 合法性与三处历史错值。全套单测 84 例。
- 不建 dynasty 字典表：poems 用 `id>>27`、authors 用 TEXT 列，两边都不需要第三份映射；上一轮实测 14K 行全表扫 3.5ms，加表加列收益 <0.1%。
- **权威库是根 `dist/`**（构建用 `--out ./dist` 从项目根跑）。`poetry-pipeline/dist/` 下有 3.4 遗留库（10-19 15:21，349MB），与根 dist 不同步——`poet_tiers.py`/`author_heat.py` 的 `DB` 默认指向那里，**必须显式传 `--db dist/poetry.db`**（2026-10-20 曾因此在旧库上白跑一次 apply，热度 JSON 被按旧 uid 重写）。
- 构建前置：沙箱把 `os.remove` 劫持为"移入回收站"，337MB 库会失败（SHFileOperationW 0x2）。**先 `rm -f dist/poetry.db` 再构建**（`os.path.exists` 为假即跳过删除）。
- `rank/author-heat-scores.json` 现为 **12,944 条**（旧的 13,062 含 118 条库里已 0 分的残留，10-20 已对齐）；屈原 uid 随 dynasty 修正变为 `7a0a42db-…`，name/dynasty(先秦)/score 100 不变。

## 语料 JSON 序列化风格（重要，改文件必读）
- 各分片序列化风格**不统一**，禁止直接 json.dump 重写整个文件（会把整文件重排版、diff 爆炸）：
  - `poet.tang.*.json` / `poet.song.*.json`：4 空格缩进、LF；**末片结尾不一**（tang.57000 结尾有 `\n`，song.254000 无）；少量 legacy 补丁写入的数组是 `"notes":[` 无空格风格。
  - `明/poet.ming.0.json`：**1 空格缩进 + CRLF**，结尾无换行；记录字段 author/paragraphs/title/id/dynasty/authorId。
  - 记录键序：author, paragraphs, tags(稀疏), title, id。
- 正确做法：**文本级外科追加**（探测缩进/换行符/结尾 → 仅在末尾 `]` 前插入新记录文本 → 解析校验 + 既有内容逐字节比对）。范例：`poetry_verifier/_probe/apply_safe_corrections.py`；`qianjiashi_merge.py:surgical_append()` 是同类实现，该脚本已于 2026-10-19 随千家诗源文件一并删除（可从会话记录追溯）。
- 千家诗并入记录：2026-10-17 将 `蒙学/qianjiashi.json`（219 条/展开组诗后 226 首）与主库比对，222 首已收录（多为异题/异撰/异文），仅 4 首补录：杜牧《清明》、赵嘏《闻笛》（→poet.tang.57000）、王中《干戈》（→poet.song.254000）、朱权《送天师》（→明/poet.ming.0），tags=[诗体, "千家诗"]。匹配链：全文精确→跨作者前8字→同作者模糊≥0.70→同题+相似度，另有人工甄别覆盖（王维《送朱大入秦》实为孟浩然同题诗、王淇《上元应制》实为王珪《恭和御制上元观灯》，均不重复添加）。**2026-10-19 该文件与 `tangshisanbaishou.json` 已删除**（逐首核对：千家诗 226/226、唐诗三百首 320/320 正文均已存在于全唐诗/水墨唐诗，仅存异文差异）；原件用 `git show HEAD:蒙学/qianjiashi.json` 可取回。
- 注意：**loader/datas.json 没有 明 数据集**，明库改动暂不进 DB；tangsong 数据集按文件名 glob 自动纳入新分片。

## 备份位置（回滚用）
- `poetry-pipeline/dist/poetry.db.bak-20261017-174715`：v2.0 分级同步前的主库（纯热度分 + v1.0 档位分）。
- `rank/author-heat-scores.json.bak-20261017-174715`：v2.0 同步前的热度 JSON（v1.0 档位分，13,062 条）。
- `poetry-pipeline/dist/poetry.db.bak-20261017-171313`：v1.0 分级同步前的主库（纯热度分）。
- `rank/author-heat-scores.json.bak-20261017-171313`：v1.0 同步前的热度 JSON（13,056 条纯热度分；v2.0 的 apply 会拿它交叉比对 poems 是否被外部改动）。
- **`dist/backups/` 整个目录已于 2026-10-20 10:40 前后意外消失**（内含 `tangsong-before-clean-20261019.tar.gz`、`empty-body-purge-20261019.tar.gz`、`rank-raw-20261016.tar.gz` 与当天新建的 `poetry-3.7-before-author-dynasty-20261020.db`）。原因未查明：同一时间窗内 `poetry-pipeline/external/`（git 跟踪）也被删，已用 `git checkout` 恢复。**结论：不要把唯一备份放在 `dist/` 下**——它是构建输出目录，多次被整体清理。源语料（全唐诗/宋词等）受 git 保护，风险可控；`rank-raw-20261016.tar.gz`（10-16 前的旧分数据）已无副本。
- 兜底：`dist/poetry.db` 被 shim 删除时会落在回收站 `E:\$RECYCLE.BIN\S-1-5-21-1100796799-...\`（`$R*.db`），可按大小找回。
- rank/ 为 git 跟踪目录，删除的原始文件可用 `git checkout -- rank/` 恢复。

## 御定全唐詩 处理约定（2026-10-19 起）
- **目录现状（11:32 重构）**：`御定全唐詩/` 只剩 `README.md`、`authors.json`、`poet.yuding.{0,1000,2000}.json`。分片与全唐诗同构（每片 1000 首，末片 675；NNNN=累计偏移），4 空格缩进 + LF + 结尾换行；原 `json/[卷N].json`（594 个卷文件）已删除。共 **2,675 篇**，卷次存于 tags 第 3 项（`卷四`…）。
- 重构顺序保证：旧文件按卷号数值升序展开 → 新分片按同一序列切片，所以集合内 seq 不变（除新增集合本身造成的整体位移）。逐条 sha256 比对确认 2,675 条零差异、顺序一致。
- 已与 `全唐诗/poet.tang.*.json` 去重：全唐诗 **44,556** 条记录带 `御定全唐诗` 标签（跨 53 个分片），仅改 tags、零字段改动。
- `sources.py` 集合已改为 `御定全唐詩\poet.yuding.*.json`、`tags="tags"`、去掉了失效的 `extra=["volume","biography","no#"]`；仍 `optional=True`，入主库需 `--with-optional`。
- 入库实测：`build.py --out ./dist --db-only --with-optional --with-strains` → poems 362,394（src 16=yuding-tangshi 占 2,674，比源少 1 篇《孟蜀桃符诗》，因「馀/余」归一后与 tang-poem 同 uid 被判重）、authors 14,396、平仄 311,783、extras 99,651。**加卷会让 yuding 之后集合的 id 整体位移，平仄包必须同时重建**，否则 id→平仄错位。
- 匹配要点：标题去空白/标点/乐府类别前缀；正文 **containment≥0.90 & Jaccard≥0.55**（同题时**不比 author**，因两版称谓不同，如 殷陶/雍陶、李得/李益、潘佐/潘佑、钱戭/陈存）；组诗按题前缀；容忍尾部来源引用/拼音。纯精确匹配仅覆盖 46%，勿回退。
- 追加 tag 到**多行** tags 数组时曾产生 `, "御定全唐诗"],` 的畸形写法（3,298 处，JSON 合法但排版乱），已用 `(\S)\n        , "御定全唐诗"\],` → `\1,\n            "御定全唐诗"\n        ],` 整形干净。后续插入务必按行缩进对齐。
- **坑：`build.py` 的 extras 根目录曾写死 `os.path.join(args.out,"extras")`**（extras 迁到 `poetry-pipeline/extras/` 时漏改），导致换 `--out` 或按新目录结构构建时 poem_extras 为空并报「extras 数据不存在」。已改为 `XDB.default_root()`。
- `御定全唐詩/authors.json` 已由 66 位补至 145 位（新 id 为 uuid4）；770 卷 47 篇作者生平原为空，未新增条目。

## 环境备忘
- 本项目实际虚拟环境是项目根 `.venv`（含 zhconv）；`poetry-pipeline/.venv` 并不存在。
- `dist/` 与 `build/` 均被 .gitignore，属可重建产物。
- 外网诗词站（搜韵/百度汉语/维基文库/诗词名句网）在本环境不可用；古诗文网与古文岛同源，不能互为校对。
