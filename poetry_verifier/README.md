# 诗词数据核对智能代理（poetry_verifier）

一个用于**核对中国古典诗词 JSON 数据**的智能代理（Agent）。它遍历目录内全部诗词 JSON，
针对每首诗词的标题、作者、朝代、正文等字段，自动检索并比对网上权威资源（默认：古文岛 /
古诗文网，二者同源），识别**缺失、错误、冲突**，并依据检索结果**修正与补全**，同时给每处修改
追加一个 `_verify` 字段标注**数据来源 URL 与变更说明**。

> 设计理念（参考 AI Agent 工程规范）：默认保守——只自动写入高置信度的“缺失/存疑字补全”和
> “缺失字段补全”；对版本异文、标题/作者冲突等**不盲目覆盖**，而是标注 `conflict_needs_review`
> 交给人工复核。所有外部调用都带超时、重试、退避、限流；自主循环有数量/时间上限；覆盖原文件
> 必须显式 `--in-place --yes`。

---

## 1. 真的需要 Agent 吗？

本任务确实需要 Agent，而不只是“一次 LLM 调用”或“固定脚本”：它要在不确定网络结果下做
**检索 → 比对 → 决策 → 写回** 的循环，且需处理不同站点的解析、繁简差异、限流与失败降级。
但我们也**没有把简单工作过度智能化**：核心的“检索+归一化+比对”是确定性代码，LLM 仅作为
可选的“裁判”增强（见下文），缺省关闭。

## 2. 架构

```
                        ┌─────────────────────────────────────────┐
                        │                 CLI / 配置              │
                        └───────────────┬─────────────────────────┘
                                        │
                        ┌───────────────▼─────────────────────────┐
                        │             Orchestrator（编排/循环）     │
                        │  · 遍历目录 · 断点续跑 · max_poems/max_time │
                        │  · 限流/重试 · 落盘 · 报告/变更日志        │
                        └───┬───────────────┬───────────────┬──────┘
                            │               │               │
                  ┌─────────▼──┐   ┌─────────▼─────┐  ┌──────▼──────┐
                  │ 发现/抽取   │   │   检索层       │  │  核对/决策   │
                  │ schemas.py │   │ retrievers/   │  │ verifier.py  │
                  │ (识别结构)  │   │ gushiwen /    │  │ (归一/比对/  │
                  │            │   │ search_api    │  │  占位符补全) │
                  └─────────┬──┘   └─────────┬─────┘  └──────┬──────┘
                            │               │               │
                            └───────────────┼───────────────┘
                                            ▼
                                  ┌──────────────────┐
                                  │  标注/写回         │
                                  │  annotator.py     │
                                  │  (追加 _verify)    │
                                  └──────────────────┘
```

| 组件 | 文件 | 职责 | 失败处理 |
|---|---|---|---|
| 配置 | `config.py` | 集中全部可调参数；密钥仅来自环境变量 | — |
| 数据契约 | `schemas.py` | 识别诗词集合、抽取字段、写回（仅追加 `_verify`） | 跳过无法识别的文件 |
| 检索 | `retrievers/gushiwen.py` | 古文岛：作者诗列表 + 标题匹配 + 诗篇页解析 | 登录墙/空页 → 降级为无参考 |
| 检索(可选) | `retrievers/search_api.py` | 联网搜索 API，用于无作者 corpus（诗经/楚辞等） | 无 Key → 关闭 |
| 网络 | `retrievers/base.py` | 超时 + 重试 + 指数退避 + 限流 + 磁盘缓存 | 重试耗尽 → 降级 |
| 核对 | `verifier.py` | 繁简归一、比对、占位符精准补全、冲突识别、错配拦截 | 无法可靠补全 → 不臆造 |
| 裁判(可选) | `llm.py` | OpenAI 兼容端点，裁决“冲突”是否覆盖 | 调用失败 → 回退确定性策略 |
| 复核通道(可选) | `review_channel.py` | 对主裁判留下的冲突，调辅助模型给“第二意见”（仅参考，绝不落盘） | 调用失败 → 该项标记 api_error |
| 标注 | `annotator.py` | 写回已批准修改 + `_verify` 标注 | — |
| 编排 | `orchestrator.py` | 有界循环、断点、报告 | 单首异常 → 记录并继续 |
| 入口 | `cli.py` | 参数解析、安全检查、运行 | — |

## 3. 安装

依赖仅 `requests` + `zhconv`（繁简转换，项目已固定版本）。建议用独立虚拟环境：

```bash
# 在项目根目录（E:\chinese_poetry_iterate）下
"你的python" -m venv poetry_verifier/.venv
poetry_verifier/.venv/Scripts/python.exe -m pip install -r poetry_verifier/requirements.txt
```

> 注：本仓库自带的 `.venv` 在某环境下解释器已损坏，故为代理单独建了一个 venv。
> 你也可用自己机器上的 Python 3.11+ 建 venv。

## 4. 运行

```bash
cd E:\chinese_poetry_iterate
PY=poetry_verifier/.venv/Scripts/python.exe

# 0) 来源可达性自检
$PY -m poetry_verifier.cli --health

# 1) 竖切片预览（默认只写 verify_output 副本，绝不改动原文件）
$PY -m poetry_verifier.cli --file "全唐诗/error/poet.tang.0.json" --limit 20

# 2) 对某文集全量预览
$PY -m poetry_verifier.cli --collections 宋词 --limit 300

# 3) 人工复核 changelog.md / report.json 后，确认覆盖原文件（危险！需 --yes）
$PY -m poetry_verifier.cli --collections 宋词 --in-place --yes
```

### 常用参数

| 参数 | 说明 |
|---|---|
| `--file PATH` | 只处理单个 JSON（相对 root 或绝对路径），便于测试/精修 |
| `--collections a,b` | 只处理指定顶层文集（如 `宋词,全唐诗`） |
| `--limit N` | 本次最多处理多少首（0=不限，配合 `--max-time`） |
| `--max-time S` | 墙钟上限（秒） |
| `--delay S` | 请求间礼貌间隔（限流，默认 1.2s） |
| `--max-pages N` | 作者诗列表最多翻几页（默认 8；提高可扩大覆盖但增加抓取量） |
| `--confidence F` | 自动写入的最低置信度（默认 0.80） |
| `--no-fill` | 关闭“缺失/存疑字（□、[..]）补全” |
| `--add-fields` | 允许把缺失字段（如朝代）真正写入（默认仅标注） |
| `--overwrite` | 允许自动覆盖“冲突”字段（默认仅标注复核） |
| `--llm` + `--llm-model M` | 启用 LLM 裁判（需配置 API Key，见下） |
| `--search-url / --search-engine` | 配置联网搜索 API（用于无作者 corpus） |
| `--in-place --yes` | 直接覆盖原文件（**危险，需二次确认**） |
| `--no-resume` | 忽略断点，从头跑 |

## 5. 输出

- `verify_output/<相对路径>`：与原目录/字段结构**完全一致**的 JSON，仅**追加** `_verify` 字段。
- `verify_output/report.json`：结构化统计（状态分布、字段修正数、来源、冲突清单、错误）。
- `verify_output/changelog.md`：人类可读的“已修正/补全”与“待人工复核（冲突）”清单。
- `verify_output/progress.json`：断点，支持 `--resume` 续跑（默认开启）。

每首诗词的 `_verify` 结构示例：

```json
{
  "checked_at": "2026-10-17T...Z",
  "source": {"name": "古文岛(gushiwen.cn)", "url": "https://www.gushiwen.cn/shiwenv_xxx.aspx"},
  "reference": {"title": "將進酒", "author": "李白", "dynasty": "唐", "content": ["..."]},
  "status": "corrected",
  "changes": [
    {"field": "paragraphs", "old": ["...不復□。"], "new": ["...不復回。"],
     "reason": "补全缺失/存疑字（□/[..] → 权威来源原文）", "confidence": 0.9, "applied": true}
  ],
  "notes": []
}
```

## 6. 重要约束与已知限制（务必阅读）

1. **权威来源单一且受限**：默认只用**古文岛（古诗文网同源）**。其**站内搜索已登录墙**，无法程序化搜索；
   因此本代理用“**作者诗列表 + 标题匹配**”定位诗篇。这意味着：
   - **无作者的 corpus（诗经、楚辞、四书五经、蒙学、幽梦影、论语等）无法定位**——除非你配置
     联网搜索 API（`--search-url`）。这些会被标记为 `no_reference`，**不会臆造**。
   - **帝王/“不詳”等作者**在古文岛没有作者主页 → `no_reference`。
   - **名家 prolific 作者**（如李白 1126 首）的诗篇若排在分页较后，超出 `--max-pages` 范围也会
     漏匹配。提高 `--max-pages` 可扩大覆盖，但会显著增加抓取量与耗时。
2. **繁简差异已处理**：本地语料为繁体，古文岛为简体，比对前统一转繁体再去标点，避免把“繁简差异”
   误判为“内容冲突”。
3. **保守写入**：仅高置信度的“缺失/存疑字补全”和“缺失字段补全”自动写入；标题/作者/正文的
   “版本异文冲突”默认进入 `conflict_needs_review`，**不自动覆盖**（可用 `--overwrite` 或启用 LLM 裁判
   在人工知情下处理）。
4. **同词牌多首消歧 + 错配拦截（防误改）**：古文学里同一作者常有**多首同词牌/同标题**的词
   （如潘阆十首《酒泉子》、柳永十余首《少年游》）。原检索只按标题匹配、取第一个，会把
   “同词牌的**另一首**”误当成本首的参考，进而可能把正确的本地词整首覆盖成另一首。

   检索层 `gushiwen.py` 现已做**首句对齐消歧**：当某标题/词牌命中多个候选时，用本地正文**首句**与
   各候选首句做相似度比对（阈值 0.6 且需明显最优），锁定**正确的那一首**再做核验。实测潘阆《酒泉子》
   的“长忆孤山/长忆观潮/长忆西山”三首已被精确命中并正确核验。

   若**古文岛本身未收录该变体**（如柳永《少年游》在作者页只返回约 40 首、未列出其余变体；寇准《江南春》、
   钱惟演《木兰花》同理），消歧无法可靠定位，则判为 `reference_mismatch` 并**跳过正文覆盖**，进入
   `changelog.md` 的“参考疑似错配”清单交人工核对篇目。该拦截在裁判之前生效，任何模型都无法越权覆盖，
   从根上杜绝灾难性误改。换言之：**能定位的尽量核验，定位不到的绝不臆断/错配。**
5. **不要并行狂刷**：请保持合理的 `--delay`，避免被目标站点限流/封禁。首次全量前务必先小批量预览。

## 7. 启用 LLM 裁判（可选）

对“冲突”类修改做智能裁决（是否用权威值覆盖本地值），需要自备 OpenAI 兼容端点的 Key：

```bash
set POETRY_VERIFY_LLM_API_KEY=sk-xxxx
# 默认即 deepseek-chat（base_url 已指向 api.deepseek.com/v1），可直接：
$PY -m poetry_verifier.cli --collections 宋词 --limit 50 --llm
# 若改用其他供应商，覆盖 model/base_url：
$PY -m poetry_verifier.cli --collections 宋词 --limit 50 --llm --llm-model qwen-max --llm-base https://dashscope.aliyuncs.com/compatible-mode/v1
```

- **默认裁判用 `deepseek-chat`**（性价比首选：中文推理强、价格极低、JSON 稳定、对“错配/异文”判断保守安全）。
- 端点/模型/价格以供应商文档为准（**模型名、价格、上下文长度属[待核实]项，请按实际填**）。
- 调用失败不致命，自动回退到确定性策略（冲突仍进入复核）。

### 模型选型与安全边界（实测结论）

- **`deepseek-chat` 作默认裁判**：在宋词 150 样本上 132 次裁决、≈$0.015；对“不同作品错配”能识别并复核，对版本异文保守——**可作自动裁决 + 自动写入（在置信度阈值内）**。
- **`glm-4-flash`（免费额度）可作“人工复核通道”的第二意见，但不建议作自动覆盖裁判**：实测它对
  参考错配（不同作品）缺乏识别，会接受“用另一首词覆盖正确词”的灾难性覆盖；对缺失字段补全又过度保守。
  因此本仓库约定：**自动裁决默认 deepseek；glm-4-flash 仅用于下方“复核通道”给人工参考，绝不自动落盘。**

## 7.1 人工复核通道（glm-4-flash 第二意见）

主裁判（deepseek）跑完后会留下一批待复核冲突。用辅助模型再给一次独立判断，供人工决策参考
（**只读 report.json，绝不修改任何诗词文件，也绝不把辅助模型的 accept 直接写入**）：

```bash
set POETRY_VERIFY_LLM_API_KEY=<智谱Key>   # glm-4-flash 的 Key
$PY -m poetry_verifier.review_channel \
    --report verify_output/report.json \
    --out verify_output/review_opinions.json \
    --model glm-4-flash --base https://open.bigmodel.cn/api/paas/v4
```

- 输出 `review_opinions.json`（结构化：每条冲突 + 辅助模型 accept/reason/confidence）与 `review_opinions.md`（人类可读复核单）。
- 若某条辅助模型调用失败，该项标记 `api_error`，不影响其它条。
- 配合第 4 节的错配拦截，复核通道面对的冲突已不含“不同作品错配”，因此 glm 的第二意见是安全的辅助信息。

## 8. 启用联网搜索（无作者 corpus）

为诗经/楚辞等无作者诗词定位参考，提供一个搜索 API（Tavily 默认；或任意 `GET ?q=` + Bearer 的通用接口）：

```bash
set POETRY_VERIFY_SEARCH_API_KEY=xxx
$PY -m poetry_verifier.cli --collections 诗经 --limit 50 \
    --search-url "https://api.tavily.com/search" --search-engine tavily
```

搜索结果中匹配到的古文岛诗篇 URL 仍交给 `gushiwen.py` 解析，保证解析逻辑单一。
Bing / SerpAPI 的真实请求/响应字段请按官方文档替换（**[待核实]**）。

## 9. 安全与合规

- 凭据（搜索 Key、LLM Key）**只来自环境变量**，不落盘、不打日志（C6）。
- 覆盖原文件默认关闭，必须 `--in-place --yes` 且建议先备份/用 git（C6）。
- 占位符补全采用“锚点对齐回填”，无法可靠定位时**绝不臆造字符**。
