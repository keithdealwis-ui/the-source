// Service worker: keeps the cached read surface fresh and hands Chrome the detection
// rules. Chrome evaluates declarativeContent rules itself; the extension is never told
// which page is open. On a matching page the toolbar icon switches to one showing the
// number of recommendations, and reverts when the page stops matching.

import { refresh, loadCached, iconCounts } from "./data.js";
import { ruleToUrlFilters } from "./match.js";

const ALARM = "refresh-surface";
const REFRESH_MINUTES = 6 * 60;
const SIZES = [16, 32];

async function badgeImageData(count) {
  const n = Math.max(1, Math.min(5, count));
  const out = {};
  for (const size of SIZES) {
    const res = await fetch(chrome.runtime.getURL(`icons/badge-${n}-${size}.png`));
    const bmp = await createImageBitmap(await res.blob());
    const canvas = new OffscreenCanvas(size, size);
    const ctx = canvas.getContext("2d");
    ctx.drawImage(bmp, 0, 0, size, size);
    out[size] = ctx.getImageData(0, 0, size, size);
  }
  return out;
}

async function registerRules(surface) {
  const dc = chrome.declarativeContent;
  await new Promise((resolve) => dc.onPageChanged.removeRules(undefined, resolve));
  if (!surface) return 0;
  const counts = iconCounts(surface);
  const bySaas = {};
  for (const rule of surface.domains.rules) {
    if (!counts[rule.saas_id]) continue;
    (bySaas[rule.saas_id] ||= []).push(...ruleToUrlFilters(rule));
  }
  const rules = [];
  for (const [saasId, filters] of Object.entries(bySaas)) {
    rules.push({
      id: saasId,
      conditions: filters.map((pageUrl) => new dc.PageStateMatcher({ pageUrl })),
      actions: [new dc.SetIcon({ imageData: await badgeImageData(counts[saasId]) })],
    });
  }
  await new Promise((resolve, reject) =>
    dc.onPageChanged.addRules(rules, () =>
      chrome.runtime.lastError ? reject(new Error(chrome.runtime.lastError.message)) : resolve()
    )
  );
  await chrome.storage.local.set({ rules_version: surface.dataset_version });
  return rules.length;
}

async function sync({ force = false } = {}) {
  const store = chrome.storage.local;
  const { surface, status } = await loadCached(store);
  const due = force || !surface || !status.last_attempt || Date.now() - status.last_attempt > REFRESH_MINUTES * 60000;
  let current = surface;
  let refreshed = false;
  if (due) {
    const r = await refresh(store, fetch);
    if (r.ok) current = r.surface;
    refreshed = r.ok;
  }
  const { rules_version } = await store.get("rules_version");
  if (current && (force || rules_version !== current.dataset_version)) {
    try {
      await registerRules(current);
    } catch (err) {
      await store.set({ rules_error: String(err.message || err) });
      throw err;
    }
  }
  return { version: current ? current.dataset_version : null, refreshed };
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(ALARM, { periodInMinutes: REFRESH_MINUTES, delayInMinutes: REFRESH_MINUTES });
  sync({ force: true }).catch((e) => console.warn("The Source: initial sync failed", e));
});

chrome.runtime.onStartup.addListener(() => {
  sync().catch((e) => console.warn("The Source: startup sync failed", e));
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) sync({ force: true }).catch((e) => console.warn("The Source: refresh failed", e));
});

// The popup asks for a refresh when it has no cache or the user presses Retry.
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (sender.id !== chrome.runtime.id || !msg || msg.type !== "refresh") return false;
  sync({ force: true })
    .then(({ version, refreshed }) => reply({ ok: refreshed, has_data: version !== null }))
    .catch((e) => reply({ ok: false, error: String(e.message || e) }));
  return true;
});
