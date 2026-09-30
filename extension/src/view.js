// What the popup should show, as plain data. Kept free of DOM and chrome.* so every
// state (match, no match, offline, error, stale, short list) is testable in Node.

import { matchUrl } from "./match.js";
import { displayable, compareUrl, isStale } from "./data.js";

export function formatStars(n) {
  if (typeof n !== "number" || !Number.isFinite(n)) return null;
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 100000 ? 0 : 1).replace(/\.0$/, "")}k`;
  return String(n);
}

export function formatDate(iso) {
  const t = Date.parse(iso || "");
  if (!Number.isFinite(t)) return null;
  return new Date(t).toISOString().slice(0, 10);
}

function recView(r) {
  const fit = r.replacement_fit && typeof r.replacement_fit.score === "number" ? Math.round(r.replacement_fit.score) : null;
  const selfHost = r.self_hosting ? r.self_hosting.self_hostable : null;
  return {
    name: r.name,
    github_url: r.github_url,
    fit,
    stars: formatStars(r.stars),
    maintenance: r.maintenance_status === "active" ? "Active" : r.maintenance_status === "maintained" ? "Maintained" : null,
    licence: r.licence_spdx || "Licence unknown",
    self_hostable: selfHost === true ? "Self-hostable" : "Self-hosting unconfirmed",
    self_hostable_ok: selfHost === true,
    note: r.note && typeof r.note.text === "string" && r.note.text ? r.note.text : null,
    note_derived: !!(r.note && r.note.origin === "derived"),
  };
}

// kind: "loading" | "error" | "no_match" | "no_recommendations" | "match"
export function buildView({ surface, status = {}, url, now = Date.now(), refreshing = false }) {
  const footer = [];
  const notices = [];
  if (surface) {
    const checked = formatDate(surface.index.live_checked_at && surface.index.live_checked_at.newest);
    if (checked) footer.push(`GitHub data checked ${checked}`);
    if (isStale(surface, now)) notices.push("This data is more than 14 days old.");
    if (status.last_error && (!status.last_ok || status.last_attempt > status.last_ok)) {
      notices.push("Couldn't reach The Source. Showing the last saved copy.");
    }
  }

  if (!surface) {
    if (refreshing) return { kind: "loading", notices, footer };
    return {
      kind: "error",
      message: "Couldn't load recommendations. Check your connection and try again.",
      retry: true,
      notices,
      footer,
    };
  }

  const saasId = url ? matchUrl(url, surface.domains.rules) : null;
  const row = saasId ? surface.index.supported_saas.find((r) => r.saas_id === saasId) : null;
  if (!row) {
    return {
      kind: "no_match",
      message: "No open-source alternatives tracked for this site.",
      supported: surface.index.supported_saas.map((r) => r.name).filter(Boolean),
      notices,
      footer,
    };
  }

  const recs = displayable(surface.products[saasId]).map(recView);
  const base = { saas_id: saasId, product: row.name || saasId, compare_url: compareUrl(row), notices, footer };
  if (recs.length === 0) {
    return { ...base, kind: "no_recommendations", message: `No credible open-source alternatives to ${base.product} right now.` };
  }
  return { ...base, kind: "match", recs, short_list: recs.length < 3 };
}
