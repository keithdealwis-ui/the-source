// Fetching and caching the canonical read surface (docs/READ-API.md).
//
// The extension downloads the WHOLE surface (index, domain rules and every product
// file, about 170 KB) on a timer, never per page. What it requests is therefore the
// same whatever the user browses, and nothing about their browsing leaves the machine.
// A snapshot replaces the cached one only when every file arrived, validated and
// carried the same dataset_version; otherwise the last good copy stays in use.

export const API_BASES = [
  "https://raw.githubusercontent.com/keithdealwis-ui/the-source/main/api/v1/",
  "https://cdn.jsdelivr.net/gh/keithdealwis-ui/the-source@main/api/v1/",
];
export const COMPARE_ORIGIN = "https://de-alwis.com";
export const STALE_AFTER_MS = 14 * 24 * 3600 * 1000;
export const FETCH_TIMEOUT_MS = 10000;

const SAAS_PATH = /^saas\/[a-z0-9-]+\.json$/;
const COMPARE_PATH = /^\/open-source\/[a-z0-9-]+$/;
const GITHUB_URL = /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/?$/;

class SurfaceError extends Error {}

async function getJson(fetchFn, url) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetchFn(url, {
      signal: ctl.signal,
      cache: "no-cache",
      credentials: "omit",
      referrerPolicy: "no-referrer",
    });
    if (!res.ok) throw new SurfaceError(`HTTP ${res.status} for ${url}`);
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

function check(cond, msg) {
  if (!cond) throw new SurfaceError(msg);
}

function validateIndex(index) {
  check(index && index.api_version === "v1", "index.json: api_version is not v1");
  check(/^[0-9a-f]{16}$/.test(index.dataset_version || ""), "index.json: bad dataset_version");
  check(Array.isArray(index.supported_saas), "index.json: supported_saas missing");
  for (const row of index.supported_saas) {
    check(typeof row.saas_id === "string" && SAAS_PATH.test(row.path || ""), `index.json: bad row ${row.saas_id}`);
  }
}

function validateDomains(domains, version) {
  check(domains && domains.dataset_version === version, "domains.json: dataset_version differs from index");
  check(Array.isArray(domains.rules), "domains.json: rules missing");
  for (const r of domains.rules) {
    check(typeof r.saas_id === "string", "domains.json: rule without saas_id");
    check((typeof r.host === "string") !== (typeof r.host_suffix === "string"), "domains.json: rule needs exactly one of host/host_suffix");
  }
}

function validateProduct(p, row, version) {
  check(p && p.dataset_version === version, `${row.path}: dataset_version differs from index`);
  check(Array.isArray(p.recommendations), `${row.path}: recommendations missing`);
}

// Fetch a complete snapshot from the first base that yields a consistent one.
export async function fetchSurface(fetchFn, bases = API_BASES) {
  const errors = [];
  for (const base of bases) {
    try {
      const index = await getJson(fetchFn, base + "index.json");
      validateIndex(index);
      const version = index.dataset_version;
      const domains = await getJson(fetchFn, base + "domains.json");
      validateDomains(domains, version);
      const files = await Promise.all(index.supported_saas.map((row) => getJson(fetchFn, base + row.path)));
      const products = {};
      index.supported_saas.forEach((row, i) => {
        validateProduct(files[i], row, version);
        products[row.saas_id] = files[i];
      });
      return { dataset_version: version, source: base, index, domains, products };
    } catch (err) {
      errors.push(`${base}: ${err && err.message ? err.message : err}`);
    }
  }
  throw new SurfaceError(errors.join(" | "));
}

// storage is anything with async get(key) / set(obj), i.e. chrome.storage.local.
export async function refresh(storage, fetchFn, now = Date.now()) {
  const status = { last_attempt: now };
  try {
    const snap = await fetchSurface(fetchFn);
    snap.fetched_at = now;
    await storage.set({ surface: snap, status: { ...status, last_ok: now, last_error: null } });
    return { ok: true, surface: snap };
  } catch (err) {
    const prev = (await storage.get("status")).status || {};
    await storage.set({ status: { ...prev, ...status, last_error: String(err.message || err) } });
    return { ok: false, error: String(err.message || err) };
  }
}

export async function loadCached(storage) {
  const got = await storage.get(["surface", "status"]);
  return { surface: got.surface || null, status: got.status || {} };
}

// Recommendations fit for display: at most `max`, only those whose links are safe.
export function displayable(product, max = 5) {
  return (product && Array.isArray(product.recommendations) ? product.recommendations : [])
    .filter((r) => r && typeof r.name === "string" && GITHUB_URL.test(r.github_url || ""))
    .slice(0, max);
}

// A per-product record of how many recommendations the icon should announce.
export function iconCounts(surface) {
  const out = {};
  if (!surface) return out;
  for (const row of surface.index.supported_saas) {
    const n = displayable(surface.products[row.saas_id]).length;
    if (n > 0) out[row.saas_id] = n;
  }
  return out;
}

export function compareUrl(row) {
  return row && COMPARE_PATH.test(row.compare_path || "") ? COMPARE_ORIGIN + row.compare_path : null;
}

export function isStale(surface, now = Date.now()) {
  const newest = surface && surface.index.live_checked_at && surface.index.live_checked_at.newest;
  const t = Date.parse(newest || surface?.index?.data_as_of || "");
  return Number.isFinite(t) ? now - t > STALE_AFTER_MS : true;
}
