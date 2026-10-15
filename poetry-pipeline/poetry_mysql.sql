-- =============================================================================
--  诗词库（poetry_db / poetry_strains）+ 用户库（user_db）
--  MySQL 8.0 建库脚本（DDL 部分同样适用于 MySQL 5.7 / MariaDB 10.x；
--  仅文末注释里的 JSON_TABLE 示例是 MySQL 8.0+ 语法）
-- -----------------------------------------------------------------------------
--  来源：poetry-pipeline 的 SQLite 产物
--        · dist/poetry.db          schema_version = 3.4（主库）
--        · dist/poetry-strains.db  package = strains（平仄可选包，schema 3.4）
--  表结构逐列对齐，**字段命名与含义与 SQLite 版完全一致**（包括 `desc`、
--  `n_char` 这类看起来可以"美化"的名字——下游 SQL 与代码依赖它们，不动）。
--
--  生成时间：2026-10-08
--
--  实测规模（用于定列类型与评估导入开销）：
--        poems        357,186 行   body 最长 142,263 字 / 424,029 字节
--        authors       13,939 行   `desc` 最长 1,521 字
--        rhythmics      1,462 行
--        sources           18 行
--        poem_extras   97,230 行   notes 最长 11,823 字 / 32,496 字节
--        meta              15 行
--        poem_strains 311,783 行   data 最长 1,490 字节（4bit/字打包）
--
-- -----------------------------------------------------------------------------
--  与 SQLite 版的差异 —— 全是 MySQL 方言/能力差异，不是语义变化：
--
--   1. 整数：按实测值域选型
--        id        INT UNSIGNED   —— id 是 int32 高位编码，最大 1,207,971,399；
--                                    自建号段（dynasty=15）理论上限 ~2.13e9，
--                                    也在 INT UNSIGNED 内（SIGNED 会擦边，不用）
--        score     TINYINT UNSIGNED —— 实测 0..255
--        其余计数  INT UNSIGNED
--
--   2. 文本：短字段用 VARCHAR(定长)，长白话文本用 MEDIUMTEXT
--        · TEXT 上限 65,535 字节，而 poems.body 最大 424,029 字节 —— 必须 MEDIUMTEXT
--        · 其余散文列（译文/注释/赏析/背景/小传）实际都 < 64KB，但统一用
--          MEDIUMTEXT：数据还会长，不给未来埋一根静默截断的雷
--        · 注意：MySQL 的 TEXT/MEDIUMTEXT **不支持字面量默认值**（8.0.13+ 才
--          支持 `DEFAULT ('')` 这种表达式默认值）。因此这些列声明为 NOT NULL
--          但**不写 DEFAULT** —— 写入时必须显式给值，空内容写 ''。
--          应用层（store.py 那套）本来就一律给全列，不受影响。
--
--   3. 数组列（poems.tags / poem_extras.images）仍然是 **\x1f（0x1F，Unit
--      Separator）连接的字符串**，落成 VARCHAR 而非 JSON。
--      why 不借机改成 JSON：v3.3 的约定就是"库里不混两种多值表示"，
--      SQLite 侧、query.py、app.py 全都按 \x1f 拆。换成 JSON 等于要求所有
--      消费方同时改解析方式，收益为零、风险不小。拆分示例见文末"查询参考"。
--
--   4. updated：**Unix 整数秒**（源里 2026-09 之前是 ISO 文本，v3.3 已改整数）。
--      实测区间 1790579648 ~ 1791440241（2026-09-28 ~ 2026-10-08）。
--
--   5. **不加任何 FOREIGN KEY 约束**。这不是偷懒：
--        · poems.author_uid = '' 表示"佚名"，authors 表里没有任何一行的 uid
--          是空串（实测 6,201 条），加 FK 直接导不进去
--        · poems.rhythmic_id = 0 表示"无词牌"（335,594 条），同理
--        · poem_extras 有 230 条孤儿行（uid 在 poems 里找不到，0.24%）
--      源库（SQLite）就是靠 0 / 空串哨兵 + 索引维护引用完整性的，这里保持一致；
--      要强约束的话得先插哨兵行，那是改语义，不在本次范围。
--
--   6. 字符集 utf8mb4 / utf8mb4_unicode_ci，与 user_db 那两张表统一。
--      显式写 ROW_FORMAT=DYNAMIC（MySQL 8.0 本来就是默认）：DYNAMIC 下行内放不下
--      的长值只留 20 字节指针、内容存到页外，不会撞上 InnoDB 约 8KB 的行内上限；
--      换成 COMPACT / REDUNDANT 时 authors.`desc`、poems.title 这些列很容易触界。
--
--  执行方式：
--        mysql -uroot -p < poetry_mysql.sql
--      或客户端里整段执行。脚本**幂等**：全部是 CREATE ... IF NOT EXISTS，
--      重复跑不会报错、不会动已有数据。
--      注意 IF NOT EXISTS 只作用于"表不存在则建"，**不会**把已存在的旧结构
--      改造成新结构；要换结构请先备份再 DROP。
-- =============================================================================

SET NAMES utf8mb4;
-- 严格模式：宁可让超长/缺值当场报错，也不要 MySQL 悄悄截断成半截数据
SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION';


-- =============================================================================
-- 一、诗词主库 poetry_db
-- =============================================================================
CREATE DATABASE IF NOT EXISTS `poetry_db`
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;
USE `poetry_db`;
SET NAMES utf8mb4;


-- -----------------------------------------------------------------------------
-- 1. `sources` 来源字典（18 行）
--    诗词的出处：tang-poem / song-ci / shijing / fu / qing-poetry ……
--    id 由构建期分配（**非自增**），与 poems.id 的低位序号同源，别手动改。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `sources` (
  `id`   INT UNSIGNED NOT NULL                COMMENT '来源ID（构建期分配）',
  `name` VARCHAR(64)  NOT NULL                COMMENT '来源标识，如 tang-poem / song-ci / shijing',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_sources_name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC
  COMMENT='来源字典：诗词出处的集合名';


-- -----------------------------------------------------------------------------
-- 2. `rhythmics` 词牌 / 曲牌字典（1,462 行）
--    只有词、曲才有值；诗为空（poems.rhythmic_id = 0）。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `rhythmics` (
  `id`   INT UNSIGNED NOT NULL                COMMENT '词牌ID（构建期分配）',
  `name` VARCHAR(64)  NOT NULL                COMMENT '词牌 / 曲牌名，如 清平乐 / 浣溪沙',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_rhythmics_name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC
  COMMENT='词牌 / 曲牌字典';


-- -----------------------------------------------------------------------------
-- 3. `authors` 作者表（13,939 行）
--
--    uid 与 id 是两套标识：
--      · id   —— 整数主键，构建期分配，承担表内唯一性与 n_poems 计数
--      · uid  —— 稳定标识，uuid5(姓名 \x01 朝代)，**内容派生、重建不变**
--                （推导键含朝代是硬要求：实测 275 组同名跨朝代，如唐宋各一个
--                 「严武」，只用姓名会撞成同一个 uid）
--    **作者关联一律走 uid**：SQLite 主库自 schema 3.4 起已从 poems 删掉
--    author_id 那一列（它是导入顺序的序号，源库增删一个作者就整体位移，
--    在 35 万行主表里存一份副本只会制造"两份作者标识谁说了算"的歧义）。
--    JOIN 写法：`LEFT JOIN authors a ON a.uid = p.author_uid`。
--    id 保留在这里是因为它仍是本表主键，且下游按 id 做增量同步时要用。
--
--    `desc` 是 MySQL 保留字，必须带反引号。列名不改（下游 SQL 依赖）。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `authors` (
  `id`      INT UNSIGNED NOT NULL                COMMENT '作者ID（构建期分配，非自增）',
  `uid`     CHAR(36)     NOT NULL DEFAULT ''     COMMENT '作者稳定标识 uuid5(name\\x01dynasty)，poems.author_uid 指向它（唯一的作者关联键）',
  `name`    VARCHAR(128) NOT NULL                COMMENT '姓名（实测最长 78 字）',
  `dynasty` VARCHAR(16)  NOT NULL DEFAULT ''     COMMENT '朝代原文，如 唐 / 宋 / 清 / 近现代 / 未详 / 自建',
  `desc`    MEDIUMTEXT   NOT NULL                COMMENT '小传 / 简介（保留字，SQL 中需反引号；实测最长 1,521 字）',
  `n_poems` INT UNSIGNED NOT NULL DEFAULT 0      COMMENT '名下作品数，构建期或写入层用 GROUP BY 重算',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_authors_uid` (`uid`),
  KEY `ix_authors_name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC
  COMMENT='作者表（id 为主键，uid 为内容派生的稳定标识兼关联键）';


-- -----------------------------------------------------------------------------
-- 4. `poems` 诗词主表（357,186 行）—— 全库最大的一张
--
--    id 编码（int32，4+3+24 位）：
--        id = (dynasty << 27) | (kind << 24) | seq
--        dynasty: 0未详 1先秦 2汉 3五代 4唐 5宋 6元 7明 8清 9近现代 15自建
--        kind   : 0诗 1词 2曲 3文 4经 5赋 6论著
--        seq    : 该 (朝代,类型) 分片内的序号，上限 16,777,215
--        → "给定 id 就知道它属于哪个集合"，按朝代/体裁筛选是一次主键范围扫描，
--          不需要为 dynasty / kind 单独建列和索引。
--        → 自建号段 seq 从 10,000,000 起，与构建期产物错开，避免手工录入
--          与批量导入互相覆盖。
--
--    uid：uuid5(朝代 \x01 作者 \x01 标题 \x01 正文)，内容派生、重建不变；
--         是 poem_extras 关联主键。**改标题/正文/作者都会让 uid 变化**，
--         写入层会把 poem_extras 的行跟着搬，但外部引用需人工重映射。
--
--    author_uid：作者关联的**唯一**一列（下方没有 author_id）。它等于
--          authors.uid = uuid5(姓名 \x01 朝代)，内容派生、重建不变；
--          佚名为空串 ''（authors 表里没有任何一行的 uid 是空串，
--          所以 LEFT JOIN 后自然为 NULL，等价于旧版的 author_id = 0）。
--          按作者检索/聚合一律 `LEFT JOIN authors a ON a.uid = p.author_uid`。
--          注意：作者名本身不冗余存在主表里，改作者名只需改 authors 一行。
--
--    body：简体无空格（text_form=simplified-nospace），**兼作检索列**——
--          全库没有单独的 FTS 表（meta.fts=0），搜索是对 body 做匹配，
--          所以检索词必须先转简体再查询。
--
--    tags：\x1f 连接的标签串（如 小学古诗\x1f思乡）。非空 11,975 行。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `poems` (
  `id`          INT UNSIGNED NOT NULL             COMMENT 'int32 高位编码 (dynasty<<27)|(kind<<24)|seq',
  `uid`         CHAR(36)     NOT NULL             COMMENT '内容派生稳定标识 uuid5（重建不变），与 poem_extras.uid 关联',
  `author_uid`  CHAR(36)     NOT NULL DEFAULT ''  COMMENT '作者稳定标识 = authors.uid；佚名为空串（唯一的作者关联列，v3.4 起不再有 author_id）',
  `title`       VARCHAR(255) NOT NULL DEFAULT ''  COMMENT '标题（上游按 250 字截断，实测上限即 250）',
  `rhythmic_id` INT UNSIGNED NOT NULL DEFAULT 0   COMMENT '词牌 / 曲牌ID；0 = 无（诗或未标注）',
  `src_id`      INT UNSIGNED NOT NULL             COMMENT '来源ID，指向 sources.id',
  `body`        MEDIUMTEXT   NOT NULL             COMMENT '正文，简体无空格；兼作检索列（最长 424,029 字节，必须 MEDIUMTEXT）',
  `tags`        VARCHAR(255) NOT NULL DEFAULT ''  COMMENT '标签串，\\x1f(0x1F) 分隔',
  `score`       TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '热度值，实测 0..255',
  `n_char`      INT UNSIGNED NOT NULL DEFAULT 0   COMMENT '正文字数（去空格后）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_poems_uid` (`uid`),
  KEY `ix_poems_author_uid` (`author_uid`),
  KEY `ix_poems_rhythmic`   (`rhythmic_id`),
  KEY `ix_poems_score`      (`score`),
  KEY `ix_poems_title`      (`title`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC
  COMMENT='诗词主表：正文 + 检索列 + 作者/词牌/来源关联';


-- -----------------------------------------------------------------------------
-- 5. `poem_extras` 扩展资料表（97,230 行）
--
--    权威源是 dist/extras/extras.jsonl，本表是它重放出来的**只读镜像**
--    （JSONL 里用短键 tr/nt/in/bg/ap/im，落库一律换成下列全名）。
--    写路径请走 extras.py（ExtrasStore.put / remove_field），
--    不要直接 UPDATE 本表 —— 下次 sync 会被覆盖回去。
--
--    · v3.3 起**去掉了溯源四列** source / alt_sources / license / url：
--      它们是元数据不是渲染内容，而 JSONL 权威源里一条不少（那四列在 JSONL
--      层合计 8.5 MB，url 占 4.3 MB）。只删列，不删数据。
--    · updated 由 ISO 文本改成 **Unix 整数秒**。
--    · 注释（notes）是**唯一存放处**：主库 poems 表在 v3.3 已彻底删掉 notes 列。
--
--    uid 允许出现"孤儿"（在 poems.uid 里找不到对应诗，实测 230 条 / 0.24%）：
--    外部数据先到、诗后被删，或补正文时 uid 变了。保留不删（JSONL 是权威源，
--    删了下次同步也回不来），靠"< 1%"的断言盯着异常膨胀。故同样不建外键。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `poem_extras` (
  `uid`                 CHAR(36)      NOT NULL             COMMENT '主键，与 poems.uid 一一对应（不是 poems.id：id 含序号，一重建就位移）',
  `translation`         MEDIUMTEXT    NOT NULL             COMMENT '现代汉语译文（非空 85,001 行）',
  `notes`               MEDIUMTEXT    NOT NULL             COMMENT '注释（非空 3,687 行；v3.3 起是注释的唯一存放处）',
  `introduction`        MEDIUMTEXT    NOT NULL             COMMENT '作品简介（非空 990 行）',
  `creative_background` MEDIUMTEXT    NOT NULL             COMMENT '创作背景（非空 2,003 行）',
  `appreciation`        MEDIUMTEXT    NOT NULL             COMMENT '赏析（非空 89,715 行）',
  `images`              VARCHAR(2048) NOT NULL DEFAULT ''  COMMENT '配图文件名串，\\x1f(0x1F) 分隔；**只存纯文件名**如 395f….jpg，不带到 images/ 前缀（非空 40,711 行）',
  `updated`             INT UNSIGNED  NOT NULL DEFAULT 0   COMMENT '最后更新时间，Unix 秒（不是 DATETIME）',
  PRIMARY KEY (`uid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC
  COMMENT='扩展资料：extras.jsonl 的只读镜像（译文/注释/简介/背景/赏析/配图）';


-- -----------------------------------------------------------------------------
-- 6. `meta` 元信息（15 行，key-value）
--    schema_version 是消费方判断"库里有哪些表/列"的唯一依据，务必保留。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `meta` (
  `k` VARCHAR(64) NOT NULL COMMENT '键',
  `v` TEXT        NULL     COMMENT '值（文本形态；数值也以字符串存）',
  PRIMARY KEY (`k`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC
  COMMENT='元信息：schema_version / 行数 / 构建时间 / 数据形态等';

-- 主库现行 meta 内容（导入时请一并写入，值取自 dist/poetry.db）：
--   schema_version    3.4
--   n_poems           357186
--   n_authors         13939
--   n_rhythmics       1462
--   text_form         simplified-nospace
--   fts               0
--   note              全库简体归一、无空格。body 兼作检索列，查询词需先转简体。
--                     dynasty/kind 由 id 高位反解；佚名的 author_uid=''。
--                     作者关联只走 author_uid（poems 表没有 author_id 列，v3.4 移除），
--                     写法：LEFT JOIN authors a ON a.uid = p.author_uid
--   extras_table      poem_extras
--   extras_rows       97230
--   extras_source     extras.jsonl
--   extras_orphans    230
--   notes_migrated    734
--   strains_pkg       1
--   built_at          2026-10-08T17:36:56
--   extras_synced_at  2026-10-08T17:37:11
--
-- 用 REPLACE INTO 而不是 INSERT ... ON DUPLICATE KEY UPDATE：
--   · 后者要在 8.0.20+ 用 `VALUES()`（产生弃用警告）或 8.0.19+ 的行别名语法
--     （MariaDB 不支持），两种写法各自有版本门槛；
--   · meta 只有 k/v 两列、无外键子表，REPLACE（删一行插一行）没有任何副作用，
--     且 MySQL 5.6/5.7/8.x 与 MariaDB 全部可用，重跑本脚本结果一致。
REPLACE INTO `meta` (`k`, `v`) VALUES
  ('schema_version',   '3.4'),
  ('n_poems',          '357186'),
  ('n_authors',        '13939'),
  ('n_rhythmics',      '1462'),
  ('text_form',        'simplified-nospace'),
  ('fts',              '0'),
  ('note',             '全库简体归一、无空格。body 兼作检索列，查询词需先转简体。dynasty/kind 由 id 高位反解；佚名的 author_uid=''''。平仄在独立可选包 poetry-strains.db，通过 id 关联。uid 是内容派生的稳定 uuid：外部数据（extras）一律用 uid 关联，不要用 id —— id 含序号，会因 dedup/重排而整体位移。作者关联只走 author_uid（authors.uid 的伴生列，由 姓名+朝代 派生，重建不变）：poems 表**没有 author_id 列**（v3.4 移除），写法是 LEFT JOIN authors a ON a.uid = p.author_uid。译文/注释/简介/背景/赏析/配图在 poem_extras 表（uid 关联）；poems 表**没有 notes 列** —— 注释的唯一存放处是 poem_extras.notes，构建期直接从源语料写入，不再经过主库中转。'),
  ('extras_table',     'poem_extras'),
  ('extras_rows',      '97230'),
  ('extras_source',    'extras.jsonl'),
  ('extras_orphans',   '230'),
  ('notes_migrated',   '734'),
  ('strains_pkg',      '1'),
  ('built_at',         '2026-10-08T17:36:56'),
  ('extras_synced_at', '2026-10-08T17:37:11');


-- -----------------------------------------------------------------------------
-- 7. 可选：正文全文索引（默认不建，需要时自行执行）
--
--    主库 body 就是检索列，但源实现的搜索是对 body 做子串匹配；357k 行上
--    LIKE '%…%' 走不了索引。要加速就上 MySQL 的 ngram 全文索引（中文必须带
--    WITH PARSER ngram，默认分词器对中文无效）：
--
--    ALTER TABLE `poems` ADD FULLTEXT KEY `ft_poems_body` (`body`) WITH PARSER ngram;
--    SELECT id, title FROM `poems`
--     WHERE MATCH(`body`) AGAINST ('明月' IN BOOLEAN MODE) LIMIT 20;
--
--    代价：索引体积大致与正文字节数同量级（本库 body 合计约 200 MB+），
--    且 ngram 默认 token size 为 2，单词字（如"月"）查不全。按需权衡。
-- -----------------------------------------------------------------------------


-- =============================================================================
-- 二、用户库 user_db
--    以下 3 段 DDL 按需求原文保留（1/2/3 的编号是原文自带），仅做缩进对齐。
-- =============================================================================

-- 1. 创建用户库
CREATE DATABASE IF NOT EXISTS `user_db`
DEFAULT CHARACTER SET utf8mb4
DEFAULT COLLATE utf8mb4_unicode_ci;
USE `user_db`;
SET NAMES utf8mb4;

-- 2. 用户主表
CREATE TABLE IF NOT EXISTS `user` (
`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '用户ID',
`nickname` VARCHAR(64) DEFAULT NULL COMMENT '昵称',
`avatar` VARCHAR(512) DEFAULT NULL COMMENT '头像URL',
`password_hash` VARCHAR(255) DEFAULT NULL COMMENT '密码哈希，第三方注册可为空',
`level` INT NOT NULL DEFAULT 0 COMMENT '用户等级',
`vip_expire_at` DATETIME DEFAULT NULL COMMENT 'VIP到期时间，NULL或<=NOW()表示非VIP',
`status` TINYINT NOT NULL DEFAULT 1 COMMENT '状态：1正常 0禁用 2注销',
`register_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '注册时间',
`last_login_at` DATETIME DEFAULT NULL COMMENT '最后登录时间',
`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
PRIMARY KEY (`id`),
KEY `idx_vip_expire_at` (`vip_expire_at`),
KEY `idx_register_time` (`register_time`),
KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户主表';

-- 3. 用户认证绑定表：手机号 / 微信 / QQ
CREATE TABLE IF NOT EXISTS `user_auth` (
`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
`user_id` BIGINT UNSIGNED NOT NULL COMMENT '用户ID',
`auth_type` VARCHAR(16) NOT NULL COMMENT '认证类型：phone/wechat/qq',
`app_id` VARCHAR(64) NOT NULL DEFAULT '' COMMENT '微信/QQ应用ID，手机号为空字符串',
`identifier` VARCHAR(128) NOT NULL COMMENT '手机号/openid',
`unionid` VARCHAR(128) DEFAULT NULL COMMENT '微信unionid，用于同主体多应用合并',
`credential` VARCHAR(255) DEFAULT NULL COMMENT '第三方凭证，如refresh_token，建议加密',
`is_verified` TINYINT NOT NULL DEFAULT 0 COMMENT '是否已验证：1是 0否',
`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
PRIMARY KEY (`id`),
UNIQUE KEY `uk_auth` (`auth_type`,`app_id`,`identifier`),
KEY `idx_user_id` (`user_id`),
KEY `idx_unionid` (`unionid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户认证绑定表';

-- 说明：user_db 与 poetry_db 之间**不加外键**，也不做跨库 JOIN 的强耦合。
--       如果后续要做"收藏 / 阅读记录 / 我的批注"，建议新建 `user_poem` 表，
--       用 (user_id, poem_uid) 关联 —— 存 uid 而不是 poems.id：
--         · poems.id 一重建就整体位移（低位是序号），uid 是内容派生的，重建不变
--         · 这样用户数据天然可跨库、可跨版本，重建主库不用同步用户表


-- =============================================================================
-- 三、平仄可选包 poetry_strains（可选，不需要平仄就别执行这一段）
--
--    源里是**独立的** dist/poetry-strains.db（主库不装平仄），这里同样建成
--    独立库而不是塞进 poetry_db —— 保持"可选、可单独分发"这个性质。
--    通过 id 与 poetry_db.poems.id 关联（不是 uid：平仄是逐字对齐正文的派生数据，
--    正文一改就作废，用 id 关联更直白）。
--
--    data 是 4bit/字打包的平仄串，解包时按 len 截断（奇数长度末字节补 0 nibble）：
--        0=仄 1=平 2=，3=。 4=○ 5=？ 6=通 7=未知
-- -----------------------------------------------------------------------------
CREATE DATABASE IF NOT EXISTS `poetry_strains`
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;
USE `poetry_strains`;
SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS `poem_strains` (
  `id`   INT UNSIGNED NOT NULL COMMENT '与 poetry_db.poems.id 同值',
  `data` MEDIUMBLOB   NOT NULL COMMENT '4bit/字打包的平仄（BLOB 上限 64KB 不够：最长正文 142k 字 → 71k 字节，故用 MEDIUMBLOB）',
  `len`  INT UNSIGNED NOT NULL COMMENT '平仄字符数，与正文字数 1:1；解包尾字节按它截断',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC
  COMMENT='平仄包：逐字对齐正文的平仄数据（4bit 打包）';

CREATE TABLE IF NOT EXISTS `meta` (
  `k` VARCHAR(64) NOT NULL COMMENT '键',
  `v` TEXT        NULL     COMMENT '值',
  PRIMARY KEY (`k`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC
  COMMENT='平仄包的元信息';

REPLACE INTO `meta` (`k`, `v`) VALUES
  ('schema_version', '3.4'),
  ('package',        'strains'),
  ('n_records',      '311783'),
  ('encoding',       '4bit/symbol; 0=仄 1=平 2=，3=。4=○ 5=？6=通 7=未知'),
  ('note',           '通过 id 与主库 poems.id 关联；奇数长度在末字节补 0 nibble，解包时按 len 字段截断。'),
  ('built_at',       '2026-10-08T17:37:24');


-- =============================================================================
-- 四、查询参考（**本节与下一节全是注释，不会执行**；示例里保留的 ';' 只为便于
--     复制粘贴——若你的客户端是按 ';' 粗暴切分语句的低级实现，可能误判本节，
--     真遇到就先把这两节整段删掉再执行）
-- =============================================================================
--
-- -- (1) 诗 + 作者 + 扩展资料，一次取全（三表 JOIN）
-- --     作者关联走 a.uid = p.author_uid（v3.4 起 poems 没有 author_id 列）；
-- --     佚名的 author_uid = ''，LEFT JOIN 后 a.* 自然为 NULL
-- SELECT p.id, p.uid, p.title, a.name AS author, a.dynasty,
--        pe.translation, pe.notes, pe.appreciation, pe.images
--   FROM `poetry_db`.`poems` p
--   LEFT JOIN `poetry_db`.`authors`     a  ON a.uid = p.author_uid
--   JOIN      `poetry_db`.`poem_extras` pe ON pe.uid = p.uid
--  WHERE p.id = 536879287;                      -- 李白《寄东鲁二稚子》
--
-- -- (2) 按朝代 / 体裁筛（id 高位编码 → 走主键，不用额外索引）
-- --     dynasty: 4=唐 5=宋 | kind: 0=诗 1=词
-- SELECT id, title FROM `poetry_db`.`poems`
--  WHERE (id >> 27) = 5 AND ((id >> 24) & 7) = 1 LIMIT 20;
--
-- -- (3) 某作者全部作品（走 uk_authors_uid + ix_poems_author_uid）
-- SELECT p.title FROM `poetry_db`.`poems` p
--  WHERE p.author_uid = (SELECT uid FROM `poetry_db`.`authors`
--                         WHERE name = '李白' AND dynasty = '唐' LIMIT 1);
--
-- -- (4) 拆 \x1f 列表（tags / images）—— MySQL 8.0 用 JSON_TABLE 最省事
--        （JSON_TABLE 是 MySQL 8.0+ 语法，MariaDB 无此函数，改用递归 CTE 或应用层拆）
-- SELECT t.tag
--   FROM `poetry_db`.`poems` p,
--        JSON_TABLE(CONCAT('["', REPLACE(p.tags, CHAR(31), '","'), '"]'),
--                   '$[*]' COLUMNS (tag VARCHAR(64) PATH '$')) AS t
--  WHERE p.id = 536879287 AND p.tags <> '';
--
-- -- (5) 有译文 / 有配图的诗各多少（这些列已统一"空串表示没有"，筛选很直白）
-- SELECT COUNT(*) FROM `poetry_db`.`poem_extras` WHERE translation <> '';
-- SELECT COUNT(*) FROM `poetry_db`.`poem_extras` WHERE images      <> '';
--
-- -- (6) 每日新增量（updated 是 Unix 秒，直接转日期）
-- SELECT DATE(FROM_UNIXTIME(updated)) d, COUNT(*) FROM `poetry_db`.`poem_extras`
--  GROUP BY d ORDER BY d DESC LIMIT 10;
--
-- -- (7) 作者作品数（与 authors.n_poems 对账，应完全一致）
-- --     注意 COUNT 要按 poems.author_uid 统计，a.id 只作展示（两者同源）
-- SELECT a.id, a.name, a.n_poems, COUNT(p.id) AS actual
--   FROM `poetry_db`.`authors` a LEFT JOIN `poetry_db`.`poems` p ON p.author_uid = a.uid
--  GROUP BY a.id, a.name, a.n_poems HAVING a.n_poems <> actual LIMIT 20;
--
-- -- (8) 检查三个引用完整性（应当都是 0；不为 0 说明导入不完整）
-- SELECT COUNT(*) FROM `poetry_db`.`poems` p
--   LEFT JOIN `poetry_db`.`sources` s ON s.id = p.src_id WHERE s.id IS NULL;
-- SELECT COUNT(*) FROM `poetry_db`.`poems` p
--   LEFT JOIN `poetry_db`.`authors` a ON a.uid = p.author_uid
--  WHERE p.author_uid <> '' AND a.id IS NULL;
-- SELECT COUNT(*) FROM `poetry_db`.`poems` p
--   LEFT JOIN `poetry_db`.`rhythmics` r ON r.id = p.rhythmic_id
--  WHERE p.rhythmic_id <> 0 AND r.id IS NULL;


-- =============================================================================
-- 五、导入建议（数据量大，参数不对会慢十倍甚至失败）
-- =============================================================================
--
-- 1) 服务端参数（my.cnf / SET GLOBAL，导入前调，导完可调回）
--      innodb_buffer_pool_size      建议 ≥ 2 GB（poems 单表约 250 MB 数据）
--      innodb_log_file_size         建议 ≥ 512 MB（大批量写入靠大 redo 撑）
--      max_allowed_packet           建议 64 MB（单行正文可达 424 KB，默认 4 MB 偏小）
--      innodb_flush_log_at_trx_commit = 2    导入期间放宽（崩了重导即可）
--      unique_checks / foreign_key_checks = 0  （本脚本无外键，但唯一索引仍在）
--
-- 2) 文本列没有 DEFAULT，INSERT 必须显式给值，空内容写 ''（不是 NULL）
--
-- 3) 大表用批量导入而不是逐条 INSERT：
--      · 从 SQLite 导 CSV：sqlite3 -header -csv dist/poetry.db \
--            "SELECT * FROM poems" > poems.csv
--      · 再 LOAD DATA LOCAL INFILE
--          LOAD DATA LOCAL INFILE 'poems.csv' INTO TABLE `poems`
--            CHARACTER SET utf8mb4
--            FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"'
--            LINES TERMINATED BY '\n' IGNORE 1 LINES;
--        （正文里含换行/逗号，务必 OPTIONALLY ENCLOSED BY '"'）
--      · 最后 ANALYZE TABLE `poems`, `poem_extras`, `authors`;
--
-- 4) 顺序：sources → rhythmics → authors → poems → poem_extras → meta
--      （无外键所以顺序其实自由，但按这个顺序导，导完随时能对账）
--
-- 5) 校验：用上面"四、查询参考"的 (7)(8) 两条对账，外加与 SQLite 版对行数
--      poems 357186 / authors 13939 / rhythmics 1462 / sources 18 /
--      poem_extras 97230 / poem_strains 311783
-- =============================================================================
