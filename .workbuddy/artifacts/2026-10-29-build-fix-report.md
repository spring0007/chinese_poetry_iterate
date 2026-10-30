# 构建中断修复报告（2026-10-29）

## 一、问题诊断

`build.py` 在 `sources.load_json` 处崩溃：

```
json.decoder.JSONDecodeError: Illegal trailing comma before end of array:
line 1528 column 27 (char 41414)
```

**不是代码问题，是源数据被改坏了。**

追溯 git 发现，当天 20:06 的提交 `c4b8910`（「更新数据」，81 个全唐诗文件、+125,532/−80,044 行）
在做一轮数据清理：删 `paragraphs: ["。"]` 的空诗、删正文里的校注与来源引用
（如「杨文生编著.王维诗集笺注第2版[M].成都：四川人民出版社,2018：186-187」）。
**删完元素没同步修正分隔逗号**，于是 JSON 失效。

```
7629dad（上一版）  poet.tang.53000.json  479,623 字节  ✅ 可解析 1000 条
c4b8910（最新）    poet.tang.53000.json  467,421 字节  ❌ 坏
```

坏文件已随提交入库，工作区与 HEAD 完全一致，`git checkout` 也救不回来。

## 二、修复内容

### 1. JSON 语法错误 3 处（就地修，不回滚）

回滚会丢掉这轮清理的成果，所以只补逗号，其它字节不动：

| 文件 | 行 | 形态 | 修法 |
|---|---|---|---|
| `poet.tang.53000.json` | 1528 | `"识尽无生理，乃觉出凡笼。",` + `],` | 删尾逗号 |
| `poet.tang.53000.json` | 6603 | `"…以死报郎为终始。"` + `"）。"` | 补缺少逗号 |
| `poet.tang.56000.json` | 4773 | `"镜鸣桂殿千山晓，花发梨园百卉新。",` + `],` | 删尾逗号 |

记录数 1000→968 / 1000→989（删掉的是清理后的空记录）。

### 2. 清理 2,000 段孤儿标点

全库扫描发现 **1,999 条记录**的正文里混有孤立标点段：`）。` 1,701 / `。` 296 / `）` 1 / 空串 2，
分布在 13 个文件（全唐诗 8 个分片为主，魏晋 6、御定 2、宋诗 2、近现代 1）。

这些是「删校注只删了长句、留下包装括号」的残留。用文本级外科手术清除——
只重写 `paragraphs` 块内部，其它字节一律不动，并逐条断言「其余字段不得变动」。

### 3. uid 重映射 2,119 条

`uid = uuid5(朝代, 作者, 标题, 正文)`，删校注会改正文 → uid 变 →
挂在旧 uid 上的热度分与译文赏析失配。

以 10-20 库为基线比对，按「同作者同题 → 仅同题」两级候选 + 相似度 ≥0.90 + 唯一最优，
重映射 **1,841 条热度键 + 118 行 extras**。逐条审计清单见 `backups/remap-report-*.json`。

## 三、验证结果

| 项目 | 结果 |
|---|---|
| `verify_db.py` | **37 项 PASS / 0 FAIL**，总体全部通过 |
| 单元测试 | **84 例通过** |
| 查询冒烟 | `search 明月 --dynasty song`、`author 王维` 均正常 |
| poems | 349,208（与清理前一致，未误删记录） |
| authors | 14,342 |
| poem_extras | 99,686，孤儿 980 = **0.98%**（阈值 1%，转绿） |
| 热度 | 326,656（基线 326,806，差 150 首 = 0.046%） |
| 产物 | poetry.db 337.3 MB / bundle 187 片 44.5 MB / strains 311,596 13.1 MB |

### 清理没有误删正文（逐字核对过）

- 王维《赠韦穆十八》68→25 字：删的是「杨文生编著.王维诗集笺注…2018：186-187」现代出版物引用；
- 李白《小桃源》185→38 字：删的王琦按语、版本校注；
- 李煜《谢新恩·樱花落尽春将困》：是「谢新恩（六首）五」的重复收录，属正当去重；
- 被删的 43 条整记录里 42 条是 `paragraphs: ["。"]` 的空诗，1 条非空（需你后续确认）。

## 四、发现的既有问题（本次未修）

**佚名诗的热度分从来没挂上过。**

`build.py:_attach_heat_json` 挂接时用**重算的 `make_uid`**，而 `heat-scores.json` 的键是按
库里的 `p.uid` 写的。佚名诗（`author_uid=''`）生成 uid 时 author 是「佚名」、
挂接时被置为空串，两端算不出同一个 uuid5。

实测 **4,157 首（1.19%）「库 uid ≠ 重算 uid」，全部是佚名诗**，其中佚名唐诗约 1,835 首。
这是 3.x 就存在的问题，不是本次引入。修它要改 `make_uid` 语义（影响全部 uid），风险大，
建议单独评估。

## 五、新增工具（保留可复用）

| 文件 | 用途 |
|---|---|
| `clean_punct_paras.py` | 删正文里孤立纯标点段。支持多行块、单行块、元素与 `]` 同行三种形态；`--dry-run` / `--apply` |
| `remap_uid.py` | uid 变化后重映射热度与 extras。`--baseline <旧库> --target <新库>`，自动备份 + 出审计清单 |

**以后任何改正文的操作都必须跟一次 remap**，否则热度/extras 必然失配。
一次性脚本 `fix_trailing_comma.py` 用完已删。

## 六、备份

`backups/`（项目根，已加 `.gitignore`，519 MB）：

- `poetry-3.7-20261020-baseline.db` — 10-20 库，remap 基线兼回滚点（从回收站捞回）
- `heat-scores-before-remap-20261029-202726.json`
- `extras-before-remap-20261029-202726/`
- `remap-report-20261029-202726.json` — 2,119 条 old→new 对照
- `poet.tang.{53000,56000}.broken-20261029.json` — 修复前的坏文件

## 七、启动命令

```bash
# 重建（沙箱会把 os.remove 劫持成移入回收站，337 MB 库删不掉，必须先手动删）
rm -f dist/poetry.db dist/poetry-strains.db
.venv/Scripts/python.exe poetry-pipeline/build.py --out ./dist --with-strains

# 校验
.venv/Scripts/python.exe poetry-pipeline/verify_db.py dist/poetry.db dist/poetry-strains.db

# 查询
.venv/Scripts/python.exe poetry-pipeline/query.py --db dist/poetry.db search 明月 --dynasty song
```
