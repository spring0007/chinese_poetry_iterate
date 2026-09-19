// Node / 浏览器通用：直接读 bundle 分片做检索。
//
// 运行：
//   cd examples/js && npm install && node bundle_query.mjs search 明月
//   node bundle_query.mjs load song/poem      # 只载入宋诗
//   node bundle_query.mjs info
//
// 为什么走 bundle 而不是 SQLite：Web / 小程序 / React Native 里没有 SQLite，
// 而 bundle 是 zstd 压缩的 JSONL，按朝代体裁分片，可以按需只拉唐诗那几片。

import fs from "node:fs";
import path from "node:path";
import { ZstdDec } from "fzstd";

const BUNDLE = process.env.BUNDLE_DIR ||
  path.resolve(process.cwd(), "..", "..", "dist", "bundle");

const DYN = ["未详", "先秦", "汉", "五代", "唐", "宋", "元", "明", "清", "近现代",
  "", "", "", "", "", "自建"];
const KIND = ["诗", "词", "曲", "文", "经", "赋", "论著"];

function readManifest() {
  const p = path.join(BUNDLE, "manifest.json");
  if (!fs.existsSync(p)) throw new Error("找不到 " + p + "（先跑 build.py）");
  return JSON.parse(fs.readFileSync(p, "utf8"));
}

function readShard(rel) {
  const raw = fs.readFileSync(path.join(BUNDLE, rel));
  const out = ZstdDec.decompress(new Uint8Array(raw));
  return Buffer.from(out).toString("utf8")
    .split("\n").filter(Boolean).map(JSON.parse);
}

/** 载入指定 (dynasty,kind) 的全部记录；不传则载入全部。 */
export function load(dynasty = "", kind = "") {
  const man = readManifest();
  const shards = man.shards.filter(s =>
    (!dynasty || s.dynasty === dynasty) && (!kind || s.kind === kind));
  return shards.flatMap(s => readShard(s.file));
}

/** 简易子串检索。浏览器里 34 万条全量遍历约 200ms，够用；
 *  要毫秒级就上服务端 SQLite 或自建倒排。 */
export function search(records, q) {
  return records.filter(r =>
    (r.t && r.t.includes(q)) || (r.a && r.a.includes(q)) ||
    (r.l && r.l.join("").includes(q)) || (r.r && r.r.includes(q)));
}

export function decodeId(id) {
  return { dynasty: DYN[(id >> 27) & 0xF], kind: KIND[(id >> 24) & 0x7], seq: id & 0xFFFFFF };
}

function show(rows, limit = 10) {
  for (const r of rows.slice(0, limit)) {
    const d = decodeId(r.i);
    console.log(`  [${d.dynasty}/${d.kind}] ${r.a || "佚名"} 《${r.t || ""}》 id=${r.i}`);
    console.log(`      ${(r.l || []).join("／").slice(0, 60)}`);
  }
  console.log(`  共 ${rows.length} 条`);
}

// ---- CLI ----
const [cmd, ...args] = process.argv.slice(2);
if (cmd === "info") {
  const man = readManifest();
  console.log(`schema ${man.schema_version}  压缩 ${man.compression}`);
  console.log(`共 ${man.total_records} 条 / ${man.total_shards} 片`);
  const g = new Map();
  for (const s of man.shards) {
    const k = `${s.dynasty}/${s.kind}`;
    const e = g.get(k) || { n: 0, bytes: 0, files: 0 };
    e.n += s.records; e.bytes += s.bytes; e.files++; g.set(k, e);
  }
  for (const [k, v] of [...g].sort((a, b) => b[1].bytes - a[1].bytes))
    console.log(`  ${k.padEnd(14)} ${String(v.n).padStart(7)} 条 / ${String(v.files).padStart(3)} 片 / ${(v.bytes / 1048576).toFixed(2)} MB`);
} else if (cmd === "load") {
  const [dy, kind] = (args[0] || "").split("/");
  const t = Date.now();
  const rows = load(dy, kind);
  console.log(`载入 ${rows.length} 条，用时 ${Date.now() - t} ms`);
  show(rows, 5);
} else if (cmd === "search") {
  const q = args[0];
  if (!q) { console.error("用法: node bundle_query.mjs search 关键词 [朝代/体裁]"); process.exit(2); }
  const [dy, kind] = (args[1] || "").split("/");
  const t = Date.now();
  const rows = search(load(dy, kind), q);
  console.log(`命中 ${rows.length} 条，用时 ${Date.now() - t} ms`);
  show(rows, 10);
} else {
  console.log("用法:");
  console.log("  node bundle_query.mjs info");
  console.log("  node bundle_query.mjs load tang/poem");
  console.log("  node bundle_query.mjs search 明月 [tang/poem]");
}
