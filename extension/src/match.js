// Local product detection (docs/READ-API.md, "Detecting a product").
//
// Pure functions, no chrome.* and no network: the popup uses them to decide which
// product the current tab is on, and background.js turns the same rules into
// declarativeContent conditions so Chrome itself does the matching while the user
// browses. The extension never sees a URL that does not match.

export function normaliseHost(hostname) {
  const h = String(hostname || "").toLowerCase().replace(/\.$/, "");
  return h.startsWith("www.") ? h.slice(4) : h;
}

function ruleMatches(rule, host, path) {
  if (rule.host !== undefined) {
    if (host !== rule.host) return false;
  } else if (rule.host_suffix !== undefined) {
    if (!host.endsWith(rule.host_suffix)) return false;
  } else {
    return false;
  }
  return rule.path_prefix === undefined || path.startsWith(rule.path_prefix);
}

// Returns the saas_id the URL belongs to, or null. When several rules match, one
// with a path_prefix wins; otherwise the first in file order.
export function matchUrl(url, rules) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return null;
  const host = normaliseHost(parsed.hostname);
  const path = parsed.pathname || "/";
  let first = null;
  for (const rule of rules || []) {
    if (!ruleMatches(rule, host, path)) continue;
    if (rule.path_prefix !== undefined) return rule.saas_id;
    if (first === null) first = rule.saas_id;
  }
  return first;
}

// declarativeContent UrlFilter objects equivalent to one domains.json rule.
// A `host` rule also needs its "www." form, because Chrome matches the raw hostname.
export function ruleToUrlFilters(rule) {
  const base = { schemes: ["https", "http"] };
  if (rule.path_prefix !== undefined) base.pathPrefix = rule.path_prefix;
  if (rule.host !== undefined) {
    return [
      { ...base, hostEquals: rule.host },
      { ...base, hostEquals: `www.${rule.host}` },
    ];
  }
  if (rule.host_suffix !== undefined) return [{ ...base, hostSuffix: rule.host_suffix }];
  return [];
}
