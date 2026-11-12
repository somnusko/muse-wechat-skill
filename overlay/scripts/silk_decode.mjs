// silk_decode.mjs -- test/deploy helper for voice_transcribe.py.
// Decodes one WeChat SILK_V3 file to raw s16le PCM @24 kHz.
//
//   node silk_decode.mjs <input.silk> <output.pcm>
//
// Exit codes: 0 ok, 2 input is not SILK (or unreadable), 3 decode
// failed, 4 silk-wasm not installed. Stdout is one status line;
// errors go to stderr.
//
// silk-wasm resolution order: $SILK_WASM_DIR, then
// ~/.local/share/wechat-muse/silk-wasm, then a node_modules dir next
// to this script, then plain Node resolution (NODE_PATH / cwd).
// Install once, e.g.:
//   mkdir -p ~/.local/share/wechat-muse/silk-wasm
//   cd ~/.local/share/wechat-muse/silk-wasm && npm init -y >/dev/null
//   npm install silk-wasm
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";

const [silkPath, pcmPath] = process.argv.slice(2);
if (!silkPath || !pcmPath) {
  console.error("usage: node silk_decode.mjs <input.silk> <output.pcm>");
  process.exit(2);
}

let data;
try {
  data = fs.readFileSync(silkPath);
} catch (e) {
  console.error(`cannot read ${silkPath}: ${e.message}`);
  process.exit(2);
}
if (!data.subarray(0, 7).toString("utf8").includes("#!SILK")) {
  console.error("not a SILK file (missing #!SILK magic)");
  process.exit(2);
}

const candidates = [
  process.env.SILK_WASM_DIR,
  path.join(os.homedir(), ".local", "share", "wechat-muse", "silk-wasm"),
  path.join(path.dirname(new URL(import.meta.url).pathname), "..", "node_modules"),
  path.join(process.cwd(), "node_modules"),
].filter(Boolean);

// require.resolve with explicit search paths: unlike createRequire
// from a package.json, this does not need a package.json to exist
// in the searched directory.
const req = createRequire(import.meta.url);
let silkWasm = null;
let lastErr = null;
for (const dir of candidates) {
  try {
    silkWasm = req(req.resolve("silk-wasm", { paths: [dir] }));
    break;
  } catch (e) {
    lastErr = e;
  }
}
if (!silkWasm) {
  try {
    silkWasm = req("silk-wasm");
  } catch (e) {
    lastErr = e;
  }
}
if (!silkWasm) {
  console.error(`silk-wasm not installed (last error: ${lastErr?.message})`);
  process.exit(4);
}

try {
  const result = await silkWasm.decode(data, 24000);
  if (!result || !result.data || result.data.length === 0) {
    throw new Error("decoder returned no PCM");
  }
  fs.writeFileSync(pcmPath, Buffer.from(result.data), { mode: 0o600 });
  console.log(`decoded ${data.length} bytes silk -> ${result.data.length} bytes pcm (${result.duration}ms)`);
} catch (e) {
  console.error(`silk decode failed: ${e.message}`);
  process.exit(3);
}
