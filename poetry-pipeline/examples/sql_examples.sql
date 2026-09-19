-- 直接查库用的 SQL 片段（iOS/Android/Flutter/后端/桌面 都能直接用）。
-- 库结构（schema 3.0；用 SELECT v FROM meta WHERE k='schema_version' 可核对）：
--   poems(id, author_id, title, rhythmic_id, src_id, body, tags, notes, score, n_char)
--   authors(id, name, dynasty, desc, n_poems)
--   rhythmics(id, name)      -- 词牌/曲牌
--   sources(id, name)        -- 来源集合
--   meta(k, v)
-- 平仄在独立包 poetry-strains.db（见最后一段）。
--
-- 关键约定：
--   * 没有 dynasty/kind 列，由 id 高位反解：dynasty = id>>27，kind = (id>>24)&7
--   * author_id = 0 表示佚名（LEFT JOIN 后 a.name 为 NULL）
--   * 全文用 body 列，库已全量简体归一 + 无空格，查询词必须先做同样归一
--   * body 用换行分隔多句

---------------------------------------------------------------- 1 全文检索
-- 查询词先转简体、去掉空格，再拼 '%q%'
SELECT p.id, a.name AS author, p.title, p.body, p.score
FROM poems p LEFT JOIN authors a ON a.id = p.author_id
WHERE p.body LIKE '%明月%'
ORDER BY p.score DESC, p.id
LIMIT 20 OFFSET 0;

---------------------------------------------------------------- 2 只查某个朝代
-- 唐 = 4。用 id 范围而不是额外一列：省一列存储 + 一个索引，且走主键范围扫描
SELECT p.id, p.title, p.body
FROM poems p
WHERE p.id BETWEEN 4 << 27 AND (4 << 27) + (1 << 27) - 1
  AND p.body LIKE '%明月%'
LIMIT 20;
-- SQLite 支持位移运算符（3.35+）。老版本请直接写常量 536870912 / 805306367。

---------------------------------------------------------------- 3 只查某种体裁
-- 体裁 = (id>>24)&7。poem=0 ci=1 qu=2 prose=3 classic=4 fu=5 article=6
SELECT p.id, p.title FROM poems p
WHERE ((p.id >> 24) & 7) = 1      -- 词
  AND p.body LIKE '%东风%'
LIMIT 20;

---------------------------------------------------------------- 4 按作者
SELECT p.id, p.title, p.body, p.score
FROM poems p JOIN authors a ON a.id = p.author_id
WHERE a.name = '李白'
ORDER BY p.score DESC, p.id
LIMIT 50;
-- 走 ix_authors_name + ix_poems_author，实测约 1 ms

---------------------------------------------------------------- 5 按词牌
-- 先在字典表取 id 再过滤主表，这样 ix_poems_rhythmic 才用得上。
-- 直接写 r.name = ? 会让那个索引变成摆设。
SELECT p.id, a.name, p.title, p.body
FROM poems p
LEFT JOIN authors a ON a.id = p.author_id
WHERE p.rhythmic_id = (SELECT id FROM rhythmics WHERE name = '水调歌头')
ORDER BY p.score DESC
LIMIT 50;

---------------------------------------------------------------- 6 标题：先等值再子串
-- ix_poems_title 只有等值/前缀能用上，LIKE '%x%' 用不上。
-- 应用侧先跑一次等值，没结果再跑子串，比上来就 LIKE 快两个数量级。
SELECT p.id, a.name, p.title, p.body
FROM poems p LEFT JOIN authors a ON a.id = p.author_id
WHERE p.title = '静夜思'
ORDER BY p.score DESC LIMIT 20;

SELECT p.id, p.title FROM poems p WHERE p.title LIKE '%春%' LIMIT 20;

---------------------------------------------------------------- 7 热门 / 随机
-- 热度已量化成 0-255 的 uint8，直接排序即可
SELECT p.id, a.name, p.title FROM poems p
LEFT JOIN authors a ON a.id = p.author_id
ORDER BY p.score DESC, p.id LIMIT 20;

-- 每日一首（随机）
SELECT p.id, a.name, p.title, p.body FROM poems p
LEFT JOIN authors a ON a.id = p.author_id
WHERE p.id = (SELECT id FROM poems ORDER BY RANDOM() LIMIT 1);

---------------------------------------------------------------- 8 作者联想（输入框自动补全）
SELECT id, name, dynasty, n_poems FROM authors
WHERE name LIKE '李%'
ORDER BY n_poems DESC LIMIT 20;

---------------------------------------------------------------- 9 统计
SELECT (SELECT COUNT(*) FROM poems)                       AS n_poems,
       (SELECT COUNT(*) FROM authors)                     AS n_authors,
       (SELECT COUNT(*) FROM poems WHERE author_id = 0)   AS n_anonymous,
       (SELECT COUNT(*) FROM rhythmics)                   AS n_rhythmics;

-- 各朝代分布（把 id 高位反解出来）
SELECT (p.id >> 27) AS dynasty_code, COUNT(*) AS n
FROM poems p GROUP BY (p.id >> 27) ORDER BY n DESC;

---------------------------------------------------------------- 10 自建条目（GUI / API 新增的）
SELECT p.id, a.name AS author, p.title, p.body
FROM poems p
LEFT JOIN authors a ON a.id = p.author_id
JOIN sources s ON s.id = p.src_id
WHERE s.name = 'user-added'
ORDER BY p.id DESC;
-- 删除时也认这个标记：构建期导入的数据不允许删，只能重新 build.py

---------------------------------------------------------------- 11 平仄（可选包，按 id 关联）
ATTACH DATABASE 'poetry-strains.db' AS strainsdb;

SELECT p.id, p.title, p.body, ps.data AS strains_blob, ps.len AS strains_len
FROM poems p JOIN strainsdb.poem_strains ps ON ps.id = p.id
WHERE p.id = 536870912;

-- 解包（4bit/符号）：0=仄 1=平 2=，3=。4=○ 5=？6=通 7=未知
-- 每字节两个符号，高 nibble 在前；末尾若补了 0 nibble，按 len 截断。
-- 各语言实现见 schema.py 的 unpack_strains / STRAIN_ALPHA_INV。

---------------------------------------------------------------- 12 建库后必做
ANALYZE;
-- 没有 sqlite_stat1 时，SQLite 会为了吃掉 ORDER BY score DESC 而选
-- ix_poems_score 做全索引扫描，把 title = ? 这种高选择性查询拖到 900ms+。
-- 有统计信息后降到 0.4ms。build.py 已在末尾执行，这里列出是因为：
-- 如果你自己写导入脚本，别忘了这一步。
