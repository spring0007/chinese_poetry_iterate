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

## 备份位置（回滚用）
- `poetry-pipeline/dist/poetry.db.bak-20261016-173458`：重评前的旧分库（旧 0-255 分数）。
- `poetry-pipeline/dist/backups/rank-raw-20261016.tar.gz`：rank/ 原始分片 + server.js + README。
- rank/ 为 git 跟踪目录，删除的原始文件可用 `git checkout -- rank/` 恢复。

## 环境备忘
- 管线专用虚拟环境：`poetry-pipeline/.venv`（含 zhconv/zstandard）；`dist/` 与 `build/` 均被 .gitignore，属可重建产物。
- 外网诗词站（搜韵/百度汉语/维基文库/诗词名句网）在本环境不可用；古诗文网与古文岛同源，不能互为校对。
