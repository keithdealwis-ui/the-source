// Acceptance criterion 6 (minimal, documented permissions) and 4 (no bundled data).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const manifest = JSON.parse(readFileSync(join(root, "manifest.json")));
const privacy = readFileSync(join(root, "PRIVACY.md"), "utf8");
const readme = readFileSync(join(root, "README.md"), "utf8");

test("permissions are exactly the four that carry no install warning", () => {
  assert.deepEqual([...manifest.permissions].sort(), ["activeTab", "alarms", "declarativeContent", "storage"]);
  assert.equal(manifest.host_permissions, undefined);
  assert.equal(manifest.optional_permissions, undefined);
  assert.equal(manifest.optional_host_permissions, undefined);
  assert.equal(manifest.content_scripts, undefined);
  assert.equal(manifest.web_accessible_resources, undefined);
  assert.equal(manifest.externally_connectable, undefined);
});

test("every permission is documented in README and PRIVACY", () => {
  for (const p of manifest.permissions) {
    assert.ok(readme.includes(`\`${p}\``), `README does not document ${p}`);
    assert.ok(privacy.includes(`\`${p}\``), `PRIVACY does not document ${p}`);
  }
});

test("network is limited to the canonical read surface", () => {
  const csp = manifest.content_security_policy.extension_pages;
  assert.match(csp, /connect-src 'self' https:\/\/raw\.githubusercontent\.com https:\/\/cdn\.jsdelivr\.net$/);
  assert.match(csp, /script-src 'self'/);
});

function walk(dir) {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    if (["node_modules", "dist", ".e2e", "test", "store"].includes(n)) return [];
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}

test("no dataset is bundled: the package ships code and icons only", () => {
  const files = walk(root).map((p) => p.slice(root.length));
  const json = files.filter((f) => f.endsWith(".json") && !["manifest.json", "package.json", "package-lock.json"].includes(f));
  assert.deepEqual(json, []);
  for (const f of files.filter((f) => f.endsWith(".js"))) {
    const src = readFileSync(join(root, f), "utf8");
    assert.ok(!/github\.com\/[a-z0-9-]+\/[a-z0-9-]+"/i.test(src.replace(/keithdealwis-ui\/the-source/g, "")), `${f} embeds repo URLs`);
    assert.ok(!src.includes("saas_id\":"), `${f} embeds product data`);
  }
});

test("no analytics, remote code or tracking endpoints", () => {
  for (const f of walk(root).filter((f) => /\.(js|html)$/.test(f))) {
    const src = readFileSync(f, "utf8");
    for (const bad of ["google-analytics.com", "googletagmanager", "segment.io", "mixpanel", "sendBeacon", "eval(", "new Function", "innerHTML", "chrome.history", "chrome.tabs.onUpdated", "chrome.webNavigation"]) {
      assert.ok(!src.includes(bad), `${f} contains ${bad}`);
    }
  }
});

test("store-ready manifest fields", () => {
  assert.equal(manifest.manifest_version, 3);
  assert.match(manifest.version, /^\d+\.\d+\.\d+$/);
  assert.ok(manifest.name.length <= 75);
  assert.ok(manifest.description.length <= 132, `description is ${manifest.description.length} chars`);
  for (const s of ["16", "32", "48", "128"]) statSync(join(root, manifest.icons[s]));
});
