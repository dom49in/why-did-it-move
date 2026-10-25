// Generates tests/fixtures/markovian_reference_vectors.json from the REAL TypeScript reference in the Meterless repo.
//   node --experimental-strip-types scripts/gen_markovian_vectors.mjs /path/to/meterless/engines/markovian/reference/src > tests/fixtures/markovian_reference_vectors.json
// configManager.ts and types.ts have no dependencies, so Node >= 22 runs them directly (no npm install needed).
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";

const src = resolve(process.argv[2] ?? "");
const { runEfficiency, modelTotals, resolveConfig, projectionCurve } =
  await import(pathToFileURL(resolve(src, "configManager.ts")).href);

const cfg = resolveConfig({ chunkSize: 3000, maxChunks: 12, carryoverTokens: 256, overlapTokens: 0 });
const mk = (tokens) => tokens.map((t, i) => ({ id: i + 1, tokens: t, carryover: "", content: "" }));
const tokenLists = [[105, 87, 87, 159, 30, 118, 83], [500, 420, 610, 380, 450], [1200], [300, 300, 300, 300, 300, 300, 300, 300], [], [1, 1], [4000, 10, 4000]];
const validation = [];
for (const p of [
  { chunkSize: 999 }, { chunkSize: 128001 }, { maxChunks: 0 }, { maxChunks: 33 }, { carryoverTokens: 127 },
  { carryoverTokens: 32769 }, { overlapTokens: -1 }, { overlapTokens: 4097 }, { chunkSize: 1856, carryoverTokens: 256 },
  { chunkSize: 1855, carryoverTokens: 256 }, { carryoverTokens: 0 }, { chunkSize: 1000, carryoverTokens: 128 },
]) {
  try { resolveConfig(p); validation.push({ partial: p, ok: true }); }
  catch (e) { validation.push({ partial: p, ok: false }); }
}
console.log(JSON.stringify({
  source: "engines/markovian/reference/src/configManager.ts (Meterless repo)",
  config: cfg,
  runs: tokenLists.map((t) => ({ tokens: t, result: runEfficiency(mk(t), cfg) })),
  model: [1, 2, 5, 10, 20, 32].map((n) => ({ n, result: modelTotals(n) })),
  projection: projectionCurve(cfg, 6),
  validation,
}, null, 1));
