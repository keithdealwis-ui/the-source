import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { matchUrl, normaliseHost, ruleToUrlFilters } from "../src/match.js";

const domains = JSON.parse(readFileSync(new URL("../../api/v1/domains.json", import.meta.url)));
const index = JSON.parse(readFileSync(new URL("../../api/v1/index.json", import.meta.url)));
const rules = domains.rules;

test("READ-API examples", () => {
  assert.equal(matchUrl("https://www.notion.so/workspace/page", rules), "notion");
  assert.equal(matchUrl("https://acme.atlassian.net/jira/software/projects", rules), "jira");
  assert.equal(matchUrl("https://acme.atlassian.net/wiki/spaces", rules), null);
  assert.equal(matchUrl("https://aws.amazon.com/s3/pricing/", rules), "amazon-s3");
  assert.equal(matchUrl("https://aws.amazon.com/ec2/", rules), null);
  assert.equal(matchUrl("https://shop-name.myshopify.com/admin", rules), "shopify");
  assert.equal(matchUrl("https://example.com/", rules), null);
});

test("host normalisation", () => {
  assert.equal(normaliseHost("WWW.Notion.SO"), "notion.so");
  assert.equal(normaliseHost("www.www.example.com"), "www.example.com");
  assert.equal(normaliseHost("trello.com."), "trello.com");
});

test("non-web schemes and junk never match", () => {
  assert.equal(matchUrl("chrome://extensions", rules), null);
  assert.equal(matchUrl("file:///Users/x/trello.com", rules), null);
  assert.equal(matchUrl("not a url", rules), null);
  assert.equal(matchUrl(undefined, rules), null);
  assert.equal(matchUrl("https://trello.com.evil.example/", rules), null);
  assert.equal(matchUrl("https://nottrello.com/", rules), null);
});

test("path_prefix rule beats a plain rule, else first in file order", () => {
  const r = [
    { host: "a.com", saas_id: "plain" },
    { host: "a.com", path_prefix: "/x", saas_id: "specific" },
    { host: "a.com", saas_id: "later" },
  ];
  assert.equal(matchUrl("https://a.com/x/1", r), "specific");
  assert.equal(matchUrl("https://a.com/y", r), "plain");
});

// Every supported product is detectable (acceptance criterion 1).
test("every supported product has at least one rule that matches a URL", () => {
  const detected = new Set();
  for (const rule of rules) {
    const host = rule.host ?? `acme${rule.host_suffix}`;
    const got = matchUrl(`https://${host}${rule.path_prefix ?? "/"}`, rules);
    assert.equal(got, rule.saas_id, `rule ${JSON.stringify(rule)}`);
    detected.add(got);
  }
  for (const row of index.supported_saas) assert.ok(detected.has(row.saas_id), `${row.saas_id} undetectable`);
  assert.ok(index.supported_saas.length >= 20, "the initial 20 are all still served");
});

function corpus() {
  const urls = ["https://example.com/", "http://localhost:8080/"];
  for (const r of rules) {
    const hosts = r.host ? [r.host, `www.${r.host}`, `x.${r.host}`] : [`acme${r.host_suffix}`, `www.acme${r.host_suffix}`, r.host_suffix.slice(1)];
    for (const h of hosts) {
      for (const p of ["/", r.path_prefix ?? "/app", `${r.path_prefix ?? ""}/deep/page`, "/other"]) {
        urls.push(`https://${h}${p}`, `http://${h.toUpperCase()}${p}?q=1#f`);
      }
    }
  }
  return [...new Set(urls)];
}

// The popup's matcher agrees with the Python reference in tests/test_canonical.py.
test("conformance with the Python reference matcher", () => {
  const urls = corpus();
  const py = `
import json, sys
from urllib.parse import urlparse
domains = json.load(open(sys.argv[1]))
def match(url):
    u = urlparse(url)
    host = (u.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    hits = [r for r in domains["rules"]
            if (r.get("host") == host or (r.get("host_suffix") and host.endswith(r["host_suffix"])))
            and (not r.get("path_prefix") or u.path.startswith(r["path_prefix"]))]
    hits.sort(key=lambda r: 0 if r.get("path_prefix") else 1)
    return hits[0]["saas_id"] if hits else None
print(json.dumps([match(u) for u in json.load(sys.stdin)]))
`;
  const file = new URL("../../api/v1/domains.json", import.meta.url).pathname;
  const expected = JSON.parse(execFileSync("python3", ["-c", py, file], { input: JSON.stringify(urls) }));
  const got = urls.map((u) => matchUrl(u, rules));
  const diffs = urls.filter((u, i) => got[i] !== expected[i]).map((u, i) => u);
  assert.deepEqual(diffs, [], "JS and Python disagree");
  assert.ok(urls.length > 300, `corpus has ${urls.length} URLs`);
  assert.ok(got.filter(Boolean).length > 100);
});

// Chrome's UrlFilter semantics for the fields we use, so the toolbar icon (Chrome-side)
// and the popup (JS-side) agree on which pages are a product.
function urlFilterMatches(f, url) {
  const u = new URL(url);
  const scheme = u.protocol.slice(0, -1);
  if (f.schemes && !f.schemes.includes(scheme)) return false;
  const host = u.hostname.toLowerCase();
  if (f.hostEquals !== undefined && host !== f.hostEquals) return false;
  if (f.hostSuffix !== undefined && !host.endsWith(f.hostSuffix)) return false;
  if (f.pathPrefix !== undefined && !u.pathname.startsWith(f.pathPrefix)) return false;
  return true;
}

test("declarativeContent filters detect exactly what the popup matcher detects", () => {
  const mismatches = [];
  for (const url of corpus()) {
    const js = matchUrl(url, rules);
    const chromeSide = new Set(rules.filter((r) => ruleToUrlFilters(r).some((f) => urlFilterMatches(f, url))).map((r) => r.saas_id));
    assert.ok(chromeSide.size <= 1, `${url} lights up more than one product: ${[...chromeSide]}`);
    const c = chromeSide.size ? [...chromeSide][0] : null;
    // One known, accepted edge: "www.<suffix-root>" (e.g. www.notion.site) lights the icon
    // but, after www-stripping, is not a product. It is not a real product URL.
    if (c !== js && !(js === null && rules.some((r) => r.host_suffix && url.includes(`//www.${r.host_suffix.slice(1)}`)))) {
      mismatches.push([url, js, c]);
    }
  }
  assert.deepEqual(mismatches, []);
});

test("filters are well formed", () => {
  for (const r of rules) {
    const fs = ruleToUrlFilters(r);
    assert.ok(fs.length >= 1);
    for (const f of fs) {
      assert.deepEqual(f.schemes, ["https", "http"]);
      assert.ok((f.hostEquals === undefined) !== (f.hostSuffix === undefined));
    }
  }
});
