import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { API_BASES, fetchSurface, refresh, loadCached, displayable, iconCounts, compareUrl, isStale } from "../src/data.js";
import { buildView, formatStars } from "../src/view.js";

const API = new URL("../../api/v1/", import.meta.url);
const read = (p) => JSON.parse(readFileSync(new URL(p, API)));

// A fetch that serves the committed api/v1 files under every base, with hooks to break things.
function fakeFetch({ fail = () => null, mutate = (p, j) => j } = {}) {
  const calls = [];
  const fn = async (url, opts) => {
    calls.push({ url, opts });
    const base = API_BASES.find((b) => url.startsWith(b));
    if (!base) throw new TypeError(`unexpected host ${url}`);
    const path = url.slice(base.length);
    const f = fail(base, path);
    if (f === "offline") throw new TypeError("Failed to fetch");
    if (typeof f === "number") return { ok: false, status: f, json: async () => ({}) };
    const body = mutate(path, read(path), base);
    return { ok: true, status: 200, json: async () => structuredClone(body) };
  };
  fn.calls = calls;
  return fn;
}

function memStore(init = {}) {
  const data = structuredClone(init);
  return {
    data,
    async get(keys) {
      const ks = typeof keys === "string" ? [keys] : keys;
      return Object.fromEntries(ks.filter((k) => k in data).map((k) => [k, structuredClone(data[k])]));
    },
    async set(obj) {
      Object.assign(data, structuredClone(obj));
    },
  };
}

test("fetches the whole surface: index, domains and every product file", async () => {
  const f = fakeFetch();
  const s = await fetchSurface(f);
  const n = read("index.json").supported_saas.length;
  assert.equal(Object.keys(s.products).length, n);
  assert.equal(s.source, API_BASES[0]);
  assert.equal(f.calls.length, n + 2);
  for (const c of f.calls) {
    assert.equal(c.opts.credentials, "omit");
    assert.equal(c.opts.referrerPolicy, "no-referrer");
  }
});

test("request set is independent of browsing: same URLs every refresh", async () => {
  const a = fakeFetch();
  const b = fakeFetch();
  await fetchSurface(a);
  await fetchSurface(b);
  assert.deepEqual(a.calls.map((c) => c.url), b.calls.map((c) => c.url));
});

test("a mixed-version snapshot on the first base falls over to the CDN", async () => {
  const f = fakeFetch({
    mutate: (p, j, base) => (base === API_BASES[0] && p === "saas/notion.json" ? { ...j, dataset_version: "0000000000000000" } : j),
  });
  const s = await fetchSurface(f);
  assert.equal(s.source, API_BASES[1]);
});

test("rejects a path that escapes saas/", async () => {
  const f = fakeFetch({
    mutate: (p, j) => (p === "index.json" ? { ...j, supported_saas: [{ ...j.supported_saas[0], path: "../../evil.json" }] } : j),
  });
  await assert.rejects(fetchSurface(f), /bad row/);
});

test("refresh stores a snapshot and status", async () => {
  const store = memStore();
  const r = await refresh(store, fakeFetch(), 1000);
  assert.equal(r.ok, true);
  const { surface, status } = await loadCached(store);
  assert.equal(surface.fetched_at, 1000);
  assert.equal(status.last_ok, 1000);
  assert.equal(status.last_error, null);
});

test("offline refresh keeps the last good copy and records the error", async () => {
  const store = memStore();
  await refresh(store, fakeFetch(), 1000);
  const r = await refresh(store, fakeFetch({ fail: () => "offline" }), 2000);
  assert.equal(r.ok, false);
  const { surface, status } = await loadCached(store);
  assert.equal(surface.fetched_at, 1000, "cache untouched");
  assert.equal(status.last_ok, 1000);
  assert.equal(status.last_attempt, 2000);
  assert.match(status.last_error, /Failed to fetch/);
});

test("HTTP errors on both bases are an error, never a partial snapshot", async () => {
  const store = memStore();
  const r = await refresh(store, fakeFetch({ fail: (b, p) => (p.startsWith("saas/") ? 503 : null) }), 5);
  assert.equal(r.ok, false);
  assert.match(r.error, /HTTP 503/);
  assert.equal((await loadCached(store)).surface, null);
});

test("displayable caps at 5 and drops recommendations with unsafe links", () => {
  const recs = Array.from({ length: 7 }, (_, i) => ({ name: `p${i}`, github_url: `https://github.com/o/p${i}` }));
  recs[1].github_url = "javascript:alert(1)";
  recs[2].github_url = "https://evil.example/o/p";
  const out = displayable({ recommendations: recs });
  assert.equal(out.length, 5);
  assert.ok(out.every((r) => r.github_url.startsWith("https://github.com/")));
});

test("compare links only to de-alwis.com open-source paths", () => {
  assert.equal(compareUrl({ compare_path: "/open-source/notion-alternatives" }), "https://de-alwis.com/open-source/notion-alternatives");
  assert.equal(compareUrl({ compare_path: "//evil.example/x" }), null);
  assert.equal(compareUrl({ compare_path: "/open-source/../../x" }), null);
  assert.equal(compareUrl({}), null);
});

async function realSurface() {
  return fetchSurface(fakeFetch());
}

test("icon counts: every committed product announces 3-5 (fewer only when the API says insufficient)", async () => {
  const s = await realSurface();
  const counts = iconCounts(s);
  for (const row of s.index.supported_saas) {
    const n = counts[row.saas_id] || 0;
    if (row.status === "ok") assert.ok(n >= 3 && n <= 5, `${row.saas_id}: ${n}`);
    else assert.ok(n < 3, `${row.saas_id}: ${n}`);
  }
});

test("view: a supported product shows 3-5 recommendations with every required field", async () => {
  const surface = await realSurface();
  const now = Date.parse(surface.index.data_as_of) + 3600e3;
  for (const row of surface.index.supported_saas) {
    const rule = surface.domains.rules.find((r) => r.saas_id === row.saas_id);
    const host = rule.host ?? `acme${rule.host_suffix}`;
    const v = buildView({ surface, url: `https://${host}${rule.path_prefix ?? "/"}`, now });
    if (row.status !== "ok") continue; // a product the cycle has marked insufficient is covered below
    assert.equal(v.kind, "match", row.saas_id);
    assert.ok(v.recs.length >= 3 && v.recs.length <= 5);
    assert.equal(v.compare_url, `https://de-alwis.com${row.compare_path}`);
    assert.deepEqual(v.notices, []);
    for (const r of v.recs) {
      assert.ok(r.name && r.github_url.startsWith("https://github.com/"));
      assert.ok(r.stars, "stars");
      assert.ok(["Active", "Maintained"].includes(r.maintenance), "maintenance");
      assert.ok(typeof r.fit === "number", "fit");
      assert.ok(r.licence);
      assert.ok(r.self_hostable);
    }
  }
});

test("view: no match, first-run loading, offline with no cache", async () => {
  const surface = await realSurface();
  const nm = buildView({ surface, url: "https://example.com/" });
  assert.equal(nm.kind, "no_match");
  assert.equal(nm.supported.length, surface.index.supported_saas.length);
  assert.equal(buildView({ surface, url: null }).kind, "no_match");
  assert.equal(buildView({ surface: null, refreshing: true }).kind, "loading");
  const err = buildView({ surface: null, status: { last_error: "x", last_attempt: 2 } });
  assert.equal(err.kind, "error");
  assert.equal(err.retry, true);
});

test("view: offline with cache shows the copy, flags it, and flags staleness", async () => {
  const surface = await realSurface();
  const later = Date.parse(surface.index.live_checked_at.newest) + 20 * 86400e3;
  const v = buildView({
    surface,
    status: { last_ok: 1, last_attempt: 2, last_error: "Failed to fetch" },
    url: "https://trello.com/b/x",
    now: later,
  });
  assert.equal(v.kind, "match");
  assert.equal(v.notices.length, 2);
  assert.ok(isStale(surface, later));
  assert.ok(v.footer[0].startsWith("GitHub data checked "));
});

test("view: short list and empty product never pad", async () => {
  const surface = await realSurface();
  surface.products.trello = { ...surface.products.trello, recommendations: surface.products.trello.recommendations.slice(0, 2), status: "insufficient_recommendations" };
  const v = buildView({ surface, url: "https://trello.com/" });
  assert.equal(v.recs.length, 2);
  assert.equal(v.short_list, true);
  surface.products.trello.recommendations = [];
  assert.equal(buildView({ surface, url: "https://trello.com/" }).kind, "no_recommendations");
});

test("formatting", () => {
  assert.equal(formatStars(34153), "34.2k");
  assert.equal(formatStars(1000), "1k");
  assert.equal(formatStars(123456), "123k");
  assert.equal(formatStars(999), "999");
  assert.equal(formatStars(undefined), null);
});
