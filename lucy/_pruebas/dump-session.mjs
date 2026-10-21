// Volcado legible de una sesión DSH (.jsonl.zstd) recorriendo TODAS las frames.
// Uso: node dump-session.mjs <sesion.jsonl.zstd> [regex] [maxLineas] [outFile]
import { readFileSync, writeFileSync } from "node:fs";
import { zstdDecompressSync } from "node:zlib";

const [file, pattern, maxArg, outFile] = process.argv.slice(2);
const buf = readFileSync(file);
const MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd]);

const trozos = [];
let pos = 0;
let ultimo = -1;
while (true) {
  const idx = buf.indexOf(MAGIC, pos);
  if (idx < 0) break;
  if (idx === ultimo) {
    pos = idx + 1;
    continue;
  }
  try {
    const out = zstdDecompressSync(buf.subarray(idx));
    trozos.push(out.toString("utf8"));
    ultimo = idx;
    pos = idx + 1;
  } catch {
    pos = idx + 1;
  }
}
const texto = trozos.join("");
const lineas = texto.split("\n").filter((l) => l.trim().length > 0);
const re = pattern ? new RegExp(pattern, "i") : null;
const max = Number(maxArg ?? 200);

if (outFile) writeFileSync(outFile, texto, "utf8");

let n = 0;
for (const l of lineas) {
  if (re && !re.test(l)) continue;
  n += 1;
  if (n > max) break;
  let out = l;
  try {
    out = JSON.stringify(JSON.parse(l));
  } catch {
    /* no-JSON */
  }
  console.log(`${n}\t${out.length > 1500 ? out.slice(0, 1500) + "…" : out}`);
}
console.error(`-- ${n} coincidencias de ${lineas.length} lineas (${trozos.length} frames)`);
