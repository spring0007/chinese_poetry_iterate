// Flutter / Dart 接入片段（两种接法：本地 SQLite 与 HTTP 接口）。
//
// 这不是可直接编译的完整工程，是把关键代码集中在一处，省得你去翻三方文档。
// 依赖（pubspec.yaml）：
//   dependencies:
//     sqflite: ^2.3.0
//     path: ^1.9.0
//     http: ^1.2.0
//
// 选哪条路：
//   * 离线优先、要全文检索 -> 把 poetry.db 打进 assets，用 sqflite 查
//   * 包体积敏感、按需加载 -> 只打 bundle/，用 http 拉分片（见 _BundleClient）
//   * 想少写代码         -> 起 examples/api_server.py，直接 HTTP

import 'dart:convert';
import 'dart:typed_data';

import 'package:http/http.dart' as http;
import 'package:sqflite/sqflite.dart';

// ---------------------------------------------------------------- 数据模型
class Poem {
  final int id;
  final String title;
  final String? author; // 佚名为 null
  final String body;
  final int score;
  final String? strains;

  Poem({
    required this.id,
    required this.title,
    this.author,
    required this.body,
    this.score = 0,
    this.strains,
  });

  List<String> get lines => body.split('\n');

  // id 高位反解朝代 / 体裁，与 Python 端 schema.parse_id 保持一致
  static const _dynasty = ['未详', '先秦', '汉', '五代', '唐', '宋', '元', '明', '清',
    '近现代', '', '', '', '', '', '自建'];
  static const _kind = ['诗', '词', '曲', '文', '经', '赋', '论著'];

  String get dynasty => _dynasty[(id >> 27) & 0xF];
  String get kind => _kind[(id >> 24) & 0x7];

  factory Poem.fromMap(Map<String, Object?> m) => Poem(
        id: m['id'] as int,
        title: (m['title'] ?? '') as String,
        author: m['author'] as String?,
        body: (m['body'] ?? '') as String,
        score: (m['score'] ?? 0) as int,
      );

  factory Poem.fromJson(Map<String, dynamic> j) => Poem(
        id: j['id'] as int,
        title: (j['title'] ?? '') as String,
        author: j['author'] as String?,
        body: (j['lines'] as List).join('\n'),
        score: (j['score'] ?? 0) as int,
      );
}

// ---------------------------------------------------------------- 本地 SQLite
class PoetryDb {
  static const _dbName = 'poetry.db';
  Database? _db;

  /// assets 里的库要先拷到应用目录；110 MB 首次拷贝约 2-3 秒，放启动页做。
  Future<void> open(String path) async {
    _db = await openDatabase(path, readOnly: true);
    // 重要：库里已有 sqlite_stat1（build.py 末尾 ANALYZE 过），
    // 不要再跑一次 ANALYZE，会覆盖统计信息且很慢。
  }

  Future<void> close() async => _db?.close();

  /// 全文检索。query 必须先做与构建期相同的归一：转简体 + 去空格。
  Future<List<Poem>> search(String query, {String dynasty = '', int limit = 20}) async {
    final db = _db;
    if (db == null) throw StateError('先调用 open()');
    final sql = StringBuffer(
        'SELECT p.id, p.title, p.body, p.score, a.name AS author '
        'FROM poems p LEFT JOIN authors a ON a.id = p.author_id WHERE p.body LIKE ?');
    final args = <Object?>['%$query%'];
    if (dynasty.isNotEmpty) {
      final code = _dynastyCode[dynasty];
      if (code != null) {
        final lo = code << 27;
        sql.write(' AND p.id BETWEEN ? AND ?');
        args.addAll([lo, lo + (1 << 27) - 1]);
      }
    }
    sql.write(' ORDER BY p.score DESC, p.id LIMIT ?');
    args.add(limit);
    final rows = await db.rawQuery(sql.toString(), args);
    return rows.map(Poem.fromMap).toList();
  }

  static const _dynastyCode = {
    'unknown': 0, 'preqin': 1, 'han': 2, 'wudai': 3, 'tang': 4,
    'song': 5, 'yuan': 6, 'ming': 7, 'qing': 8, 'modern': 9, 'custom': 15,
  };

  Future<List<Poem>> byAuthor(String author, {int limit = 50}) async {
    final rows = await _db!.rawQuery(
      'SELECT p.id, p.title, p.body, p.score, a.name AS author '
      'FROM poems p JOIN authors a ON a.id = p.author_id '
      'WHERE a.name = ? ORDER BY p.score DESC, p.id LIMIT ?',
      [author, limit],
    );
    return rows.map(Poem.fromMap).toList();
  }

  /// 平仄是可选包。需要时 ATTACH，不需要就不带，省 13 MB。
  Future<String?> strainsOf(int id) async {
    final rows = await _db!.rawQuery(
      'SELECT data, len FROM strainsdb.poem_strains WHERE id = ?', [id]);
    if (rows.isEmpty) return null;
    return _unpackStrains(rows.first['data'] as Uint8List, rows.first['len'] as int);
  }

  static const _alpha = ['仄', '平', '，', '。', '○', '？', '通', '?'];

  /// 4bit/符号解包，与 Python schema.unpack_strains 完全对应
  static String _unpackStrains(Uint8List data, int len) {
    final sb = StringBuffer();
    for (final b in data) {
      sb.write(_alpha[b >> 4]);
      sb.write(_alpha[b & 0xF]);
    }
    final s = sb.toString();
    return s.length > len ? s.substring(0, len) : s;
  }

  /// 用之前：await db.rawQuery("ATTACH DATABASE '...' AS strainsdb")
  Future<void> attachStrains(String path) =>
      _db!.rawQuery('ATTACH DATABASE ? AS strainsdb', [path]);
}

// ---------------------------------------------------------------- HTTP 接口
class PoetryApi {
  final String base; // 例如 http://127.0.0.1:8787
  PoetryApi(this.base);

  Future<List<Poem>> search(String q,
      {String dynasty = '', String kind = '', int limit = 20}) async {
    final uri = Uri.parse('$base/api/search').replace(queryParameters: {
      'q': q,
      if (dynasty.isNotEmpty) 'dynasty': dynasty,
      if (kind.isNotEmpty) 'kind': kind,
      'limit': '$limit',
    });
    final r = await http.get(uri).timeout(const Duration(seconds: 10));
    if (r.statusCode != 200) throw Exception('HTTP ${r.statusCode}: ${r.body}');
    final j = jsonDecode(utf8.decode(r.bodyBytes)) as Map<String, dynamic>;
    return (j['items'] as List).map((e) => Poem.fromJson(e)).toList();
  }

  /// 新增一条。服务端需以 --allow-write 启动。
  Future<int> add({
    required List<String> lines,
    String title = '',
    String author = '',
    String dynasty = 'custom',
    String kind = 'poem',
  }) async {
    final r = await http.post(
      Uri.parse('$base/api/poem'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({
        'title': title, 'author': author, 'dynasty': dynasty,
        'kind': kind, 'lines': lines,
      }),
    ).timeout(const Duration(seconds: 10));
    if (r.statusCode != 201) throw Exception('HTTP ${r.statusCode}: ${r.body}');
    return (jsonDecode(utf8.decode(r.bodyBytes)) as Map)['id'] as int;
  }
}

// ---------------------------------------------------------------- bundle 分片
/// 走 bundle 的好处：只拉需要的朝代分片（唐诗 12 MB、宋诗 30 MB…），
/// 而不是整个 110 MB 的库。代价是需要一个 zstd 解压库（如 flutter_zstd）。
class BundleClient {
  final String base; // 例如 https://cdn.example.com/bundle/
  final Future<Uint8List> Function(Uint8List) decompress;
  Map<String, dynamic>? _manifest;
  final _cache = <String, List<Map<String, dynamic>>>{};

  BundleClient(this.base, this.decompress);

  Future<Map<String, dynamic>> manifest() async {
    if (_manifest != null) return _manifest!;
    final r = await http.get(Uri.parse('${base}manifest.json'))
        .timeout(const Duration(seconds: 15));
    _manifest = jsonDecode(utf8.decode(r.bodyBytes)) as Map<String, dynamic>;
    return _manifest!;
  }

  Future<List<Map<String, dynamic>>> loadShard(String file) async {
    if (_cache.containsKey(file)) return _cache[file]!;
    final r = await http.get(Uri.parse(base + file))
        .timeout(const Duration(seconds: 30));
    final text = utf8.decode(await decompress(r.bodyBytes));
    final rows = text
        .split('\n')
        .where((l) => l.trim().isNotEmpty)
        .map((l) => jsonDecode(l) as Map<String, dynamic>)
        .toList();
    _cache[file] = rows;
    return rows;
  }
}
