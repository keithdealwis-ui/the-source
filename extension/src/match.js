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

// A path_prefix matches whole path segments: "/wiki" matches "/wiki" and "/wiki/x",
// never "/wikipedia". A prefix that ends in "/" is a plain prefix.
export function pathMatches(prefix, path) {
  if (prefix.endsWith("/")) return path.startsWith(prefix);
  return path === prefix || path.startsWith(prefix + "/");
}

function ruleMatches(rule, host, path) {
  if (rule.host !== undefined) {
    if (host !== rule.host) return false;
  } else if (rule.host_suffix !== undefined) {
    if (!host.endsWith(rule.host_suffix)) return false;
  } else {
    return false;
  }
  return rule.path_prefix === undefined || pathMatches(rule.path_prefix, path);
}

// How specific a rule is: a path_prefix beats none, a longer path_prefix beats a shorter
// one, an exact host beats a suffix, a longer suffix beats a shorter one.
function specificity(rule) {
  const p = rule.path_prefix;
  return [p !== undefined ? 1 : 0, p ? p.length : 0, rule.host !== undefined ? 1 : 0,
    (rule.host ?? rule.host_suffix ?? "").length];
}

function moreSpecific(a, b) {
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return a[i] > b[i];
  return false;
}

// Returns the saas_id the URL belongs to, or null. When several rules match, the most
// specific wins (source_pipeline/detect.py); remaining ties go to the first in file order.
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
  let best = null;
  let bestKey = null;
  for (const rule of rules || []) {
    if (!ruleMatches(rule, host, path)) continue;
    const key = specificity(rule);
    if (bestKey === null || moreSpecific(key, bestKey)) {
      best = rule;
      bestKey = key;
    }
  }
  return best ? best.saas_id : null;
}

// declarativeContent UrlFilter objects equivalent to one domains.json rule.
// A `host` rule also needs its "www." form, because Chrome matches the raw hostname.
// A path_prefix becomes pathEquals plus pathPrefix "<prefix>/", so Chrome matches whole
// segments too. Chrome cannot express "the most specific rule wins": on a page where a
// path rule and a plain rule of different products both match (app.hubspot.com/email),
// both conditions hold, and the popup's matchUrl decides which product it shows.
export function ruleToUrlFilters(rule) {
  const bases = [];
  const schemes = ["https", "http"];
  if (rule.path_prefix === undefined) bases.push({ schemes });
  else if (rule.path_prefix.endsWith("/")) bases.push({ schemes, pathPrefix: rule.path_prefix });
  else bases.push({ schemes, pathEquals: rule.path_prefix }, { schemes, pathPrefix: `${rule.path_prefix}/` });
  const out = [];
  for (const base of bases) {
    if (rule.host !== undefined) {
      out.push({ ...base, hostEquals: rule.host }, { ...base, hostEquals: `www.${rule.host}` });
    } else if (rule.host_suffix !== undefined) {
      out.push({ ...base, hostSuffix: rule.host_suffix });
    }
  }
  return out;
}
