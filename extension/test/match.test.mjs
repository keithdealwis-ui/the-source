import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { matchUrl, normaliseHost, ruleToUrlFilters } from "../src/match.js";

const domains = JSON.parse(readFileSync(new URL("../../api/v1/domains.json", import.meta.url)));
const index = JSON.parse(readFileSync(new URL("../../api/v1/index.json", import.meta.url)));
const catalogue = JSON.parse(readFileSync(new URL("../../api/v1/catalogue.json", import.meta.url)));
const rules = domains.rules;
const catalogueRules = catalogue.entries.flatMap((e) => e.match.map((m) => ({ saas_id: e.saas_id, ...m })));
const REPO = new URL("../../", import.meta.url).pathname;

test("READ-API examples", () => {
  assert.equal(matchUrl("https://www.notion.so/workspace/page", rules), "notion");
  assert.equal(matchUrl("https://acme.atlassian.net/jira/software/projects", rules), "jira");
  assert.equal(matchUrl("https://acme.atlassian.net/wiki/spaces", rules), "confluence");
  assert.equal(matchUrl("https://acme.atlassian.net/wikipedia", rules), null);
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

test("path prefixes match whole segments; the most specific rule wins", () => {
  const r = [
    { host_suffix: ".atlassian.net", path_prefix: "/jira", saas_id: "jira" },
    { host_suffix: ".atlassian.net", path_prefix: "/jira/servicedesk", saas_id: "jsm" },
    { host_suffix: ".atlassian.net", path_prefix: "/wiki", saas_id: "confluence" },
    { host_suffix: ".sharepoint.com", saas_id: "sharepoint" },
    { host_suffix: "-my.sharepoint.com", saas_id: "onedrive" },
    { host_suffix: ".hubspot.com", saas_id: "suffix" },
    { host: "app.hubspot.com", saas_id: "hubspot" },
    { host: "app.hubspot.com", path_prefix: "/email", saas_id: "marketing" },
    { host: "b.com", path_prefix: "/dir/", saas_id: "slash" },
  ];
  assert.equal(matchUrl("https://acme.atlassian.net/wiki", r), "confluence");
  assert.equal(matchUrl("https://acme.atlassian.net/wikipedia", r), null);
  assert.equal(matchUrl("https://acme.atlassian.net/jira/software/c", r), "jira");
  assert.equal(matchUrl("https://acme.atlassian.net/jira/servicedesk/projects", r), "jsm");
  assert.equal(matchUrl("https://acme.atlassian.net/jira/servicedesks", r), "jira");
  assert.equal(matchUrl("https://contoso-my.sharepoint.com/personal", r), "onedrive");
  assert.equal(matchUrl("https://contoso.sharepoint.com/sites/x", r), "sharepoint");
  assert.equal(matchUrl("https://app.hubspot.com/contacts/1", r), "hubspot");
  assert.equal(matchUrl("https://app.hubspot.com/email/1/manage", r), "marketing");
  assert.equal(matchUrl("https://app.hubspot.com/emailing", r), "hubspot");
  assert.equal(matchUrl("https://other.hubspot.com/", r), "suffix");
  assert.equal(matchUrl("https://b.com/dir/x", r), "slash");
  assert.equal(matchUrl("https://b.com/dir", r), null);
});

// KEI-922: every catalogue entry (all of them, not only the served ones) is detected by its own rules.
test("every catalogue entry's rules detect it among all catalogue rules", () => {
  const wrong = [];
  for (const e of catalogue.entries) {
    for (const m of e.match) {
      const host = m.host ?? `acme${m.host_suffix}`;
      for (const url of [`https://${host}${m.path_prefix ?? "/"}`, `https://${host}${m.path_prefix ?? ""}/example/page`]) {
        const got = matchUrl(url, catalogueRules);
        if (got !== e.saas_id) wrong.push([url, e.saas_id, got]);
      }
    }
  }
  assert.deepEqual(wrong, []);
  assert.equal(catalogue.entries.length, catalogue.counts.entries);
  assert.ok(catalogue.entries.length >= 500);
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

function corpus(ruleSet = rules) {
  const urls = ["https://example.com/", "http://localhost:8080/"];
  for (const r of ruleSet) {
    const hosts = r.host ? [r.host, `www.${r.host}`, `x.${r.host}`] : [`acme${r.host_suffix}`, `www.acme${r.host_suffix}`, r.host_suffix.slice(1)];
    for (const h of hosts) {
      for (const p of ["/", r.path_prefix ?? "/app", `${r.path_prefix ?? ""}/deep/page`, `${r.path_prefix ?? ""}x`, "/other"]) {
        urls.push(`https://${h}${p}`, `http://${h.toUpperCase()}${p}?q=1#f`);
      }
    }
  }
  return [...new Set(urls)];
}

// The popup's matcher agrees with the Python reference in tests/test_canonical.py.
test("conformance with the Python reference matcher", () => {
  const urls = corpus();
  const expected = pythonMatch(urls, rules);
  const got = urls.map((u) => matchUrl(u, rules));
  const diffs = urls.filter((u, i) => got[i] !== expected[i]);
  assert.deepEqual(diffs, [], "JS and Python disagree");
  assert.ok(urls.length > 300, `corpus has ${urls.length} URLs`);
  assert.ok(got.filter(Boolean).length > 100);
});

test("conformance with the Python reference matcher over every catalogue rule", () => {
  const urls = corpus(catalogueRules);
  const expected = pythonMatch(urls, catalogueRules);
  const got = urls.map((u) => matchUrl(u, catalogueRules));
  const diffs = urls.filter((u, i) => got[i] !== expected[i]);
  assert.deepEqual(diffs, [], "JS and Python disagree");
  assert.ok(urls.length > 5000, `corpus has ${urls.length} URLs`);
});

// The reference is source_pipeline/detect.py itself (standard library only).
function pythonMatch(urls, ruleSet) {
  const py = `
import json, sys
sys.path.insert(0, sys.argv[1])
from source_pipeline import detect
data = json.load(sys.stdin)
print(json.dumps([detect.match_url(u, data["rules"]) for u in data["urls"]]))
`;
  return JSON.parse(execFileSync("python3", ["-S", "-c", py, REPO], {
    input: JSON.stringify({ urls, rules: ruleSet }), maxBuffer: 64 * 1024 * 1024 }));
}

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
  if (f.pathEquals !== undefined && u.pathname !== f.pathEquals) return false;
  return true;
}

test("declarativeContent filters detect exactly what the popup matcher detects", () => {
  const mismatches = [];
  for (const url of corpus()) {
    const js = matchUrl(url, rules);
    const chromeSide = new Set(rules.filter((r) => ruleToUrlFilters(r).some((f) => urlFilterMatches(f, url))).map((r) => r.saas_id));
    // Chrome cannot express "most specific wins", so where a path rule and a plain rule of two
    // products both hold (dashboard.stripe.com/billing) both light; the popup's answer must be
    // among them, and nothing may light where the popup finds no product. One known, accepted
    // edge: "www.<suffix-root>" (e.g. www.notion.site) lights the icon but, after www-stripping,
    // is not a product. It is not a real product URL.
    const wwwEdge = js === null && rules.some((r) => r.host_suffix && url.includes(`//www.${r.host_suffix.slice(1)}`));
    if (js !== null ? !chromeSide.has(js) : chromeSide.size > 0 && !wwwEdge) {
      mismatches.push([url, js, [...chromeSide]]);
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
      assert.ok(f.pathEquals === undefined || f.pathPrefix === undefined);
    }
  }
});
