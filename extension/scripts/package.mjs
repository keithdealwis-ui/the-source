// Builds dist/the-source-extension-<version>.zip for the Chrome Web Store: exactly the
// files the extension loads, nothing else. Checks what went in before reporting.
import { execFileSync } from "node:child_process";
import { readFileSync, mkdirSync, rmSync, statSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const manifest = JSON.parse(readFileSync(join(root, "manifest.json")));
const INCLUDE = ["manifest.json", "src", "popup", "icons"];

const files = INCLUDE.flatMap(function walk(rel) {
  const p = join(root, rel);
  return statSync(p).isDirectory() ? readdirSync(p).sort().flatMap((n) => walk(join(rel, n))) : [rel];
});

const dist = join(root, "dist");
mkdirSync(dist, { recursive: true });
const zip = join(dist, `the-source-extension-${manifest.version}.zip`);
rmSync(zip, { force: true });
// -X: no extra file attributes; fixed order from the sorted list.
execFileSync("zip", ["-X", "-q", zip, ...files], { cwd: root });

const listed = execFileSync("unzip", ["-Z1", zip]).toString().trim().split("\n").sort();
const expected = [...files].sort();
if (JSON.stringify(listed) !== JSON.stringify(expected)) throw new Error("zip contents differ from the file list");
for (const f of listed) {
  if (/(^|\/)(test|node_modules|\.e2e|store|scripts)\//.test(f) || f.endsWith(".map")) throw new Error(`dev file in package: ${f}`);
}
// Every file the manifest references is inside.
const refs = [
  ...Object.values(manifest.icons),
  ...Object.values(manifest.action.default_icon),
  manifest.action.default_popup,
  manifest.background.service_worker,
];
for (const r of refs) if (!listed.includes(r)) throw new Error(`manifest references missing file ${r}`);
const size = statSync(zip).size;
console.log(`${zip.slice(root.length)}  ${listed.length} files  ${(size / 1024).toFixed(1)} KB`);
console.log(listed.map((f) => `  ${f}`).join("\n"));
