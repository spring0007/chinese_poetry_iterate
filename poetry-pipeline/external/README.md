# 外接数据目录

源库（`E:\chinese-poetry-master`）里没有的集合放这里。**往这个目录丢一个 JSON 文件，
构建时自动发现，不需要改任何代码。**

## 文件格式

一个文件 = 一个集合：

```json
{
  "collection": "qing-poetry",        // 必填。集合名，落进 sources 字典表
  "label": "清代诗词",                 // 选填。中文名，仅展示
  "dynasty": "qing",                  // 必填。必须是 schema.DYNASTY 的键
  "kind": "poem",                     // 必填。必须是 schema.KIND 的键
  "default_author": "",               // 选填。整集缺作者时的补录值，猜不出就留空
  "enabled": true,                    // 选填。false 跳过（临时停用不用删文件）
  "note": "",                         // 选填。备注，不入库
  "items": [
    {
      "title": "己亥杂诗·其五",
      "author": "龚自珍",
      "lines": ["浩荡离愁白日斜，吟鞭东指即天涯。", "落红不是无情物，化作春泥更护花。"],
      "rhythmic": "",                 // 选填。词牌/曲牌
      "tags": ["七言绝句"],            // 选填
      "notes": [],                    // 选填
      "dynasty": "qing",              // 选填。逐条覆盖朝代（歌赋跨朝代时用）
      "kind": "poem",                 // 选填。逐条覆盖体裁
      "year": 1839                    // 选填。低频字段，进 extra
    }
  ]
}
```

## 可用枚举

朝代 `dynasty`：`unknown preqin han wudai tang song yuan ming qing modern custom`
体裁 `kind`：`poem ci qu prose classic fu article`

写错枚举会**直接构建失败**并指出文件和字段值——不做「猜一个默认值」的兜底。
因为 id 高 4 位是朝代、中 3 位是体裁，写错会让记录落进错误的号段，
表现为"数据明明进去了却搜不到"，是最难排查的一类问题。

## 逐条覆盖朝代/体裁

只有外接数据支持（源库集合的朝代由目录结构决定，不允许被单条改写）。
歌赋这类横跨汉唐宋的文体，一个文件就能装下，不必拆成三个文件。

## 已有文件

| 文件 | 内容 | 状态 |
|---|---|---|
| `fu.json` | 歌赋（赋） | 含 3 条公开领域节选，作格式示范 |
| `qing-poetry.json` | 清代诗词 | 含 2 条公开领域节选，作格式示范 |
| `mao-xuan.json` | 毛选（论著） | `items` 留空 + `enabled:false`，内容需自行确认版权后填充 |

## 加一个新集合的步骤

1. 复制上面任一文件改名，例如 `external/yuan-zaju.json`
2. 改 `collection` / `dynasty` / `kind`
3. 往 `items` 里填数据
4. `python build.py --out ./dist`（外接数据默认参与构建；`--no-external` 可排除）

构建日志里会出现「外接集合 N 个：…」。没出现说明文件没被扫到——检查是不是
`enabled:false`，或者顶层少了 `items` 字段。
