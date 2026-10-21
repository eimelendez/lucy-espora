// Lector mínimo de app.asar (formato Electron pickle+JSON).
// Uso: node asar-probe.mjs <ruta.asar> list [regex]
//      node asar-probe.mjs <ruta.asar> cat <ruta-interna> [maxBytes]
import { readFileSync, writeFileSync } from "node:fs";

const [asarPath, cmd, arg, arg2] = process.argv.slice(2);
const buf = readFileSync(asarPath);

const headerPickleSize = buf.readUInt32LE(4);
const headerBuf = buf.subarray(8, 8 + headerPickleSize);
const jsonSize = headerBuf.readUInt32LE(4);
const header = JSON.parse(headerBuf.toString("utf8", 8, 8 + jsonSize));
const base = 8 + headerPickleSize;

function walk(node, prefix, out) {
  for (const [name, entry] of Object.entries(node.files ?? {})) {
    const p = prefix ? `${prefix}/${name}` : name;
    if (entry.files) walk(entry, p, out);
    else out.push({ path: p, size: Number(entry.size), offset: Number(entry.offset) });
  }
  return out;
}
const files = walk(header, "", []);

if (cmd === "list") {
  const re = arg ? new RegExp(arg, "i") : null;
  const hits = files.filter((f) => !re || re.test(f.path));
  for (const f of hits) console.log(`${f.size}\t${f.path}`);
  console.log(`-- ${hits.length}/${files.length} archivos`);
} else if (cmd === "cat") {
  const f = files.find((x) => x.path === arg);
  if (!f) {
    console.error("no existe:", arg);
    process.exit(2);
  }
  const max = Number(arg2 ?? 200000);
  const body = buf.subarray(base + f.offset, base + f.offset + Math.min(f.size, max));
  writeFileSync(process.argv[6] ?? "lucy/_pruebas/asar-out.txt", body);
  console.log(`extraido ${f.size} bytes -> ${process.argv[6] ?? "lucy/_pruebas/asar-out.txt"}`);
} else {
  console.error("cmd: list | cat");
  process.exit(1);
}
