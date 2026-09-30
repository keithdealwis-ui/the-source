import { loadCached } from "../src/data.js";
import { buildView } from "../src/view.js";

const main = document.getElementById("main");
const footer = document.getElementById("footer");
const subtitle = document.getElementById("subtitle");
const actions = document.getElementById("actions");

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function link(href, text, label) {
  return el("a", { href, target: "_blank", rel: "noopener noreferrer", "aria-label": label }, text);
}

async function activeTabUrl() {
  // activeTab grants this tab's URL only because the user clicked the toolbar icon.
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab && tab.url ? tab.url : null;
}

function renderRec(r) {
  const facts = el(
    "div",
    { class: "facts" },
    r.stars ? el("span", {}, `★ ${r.stars}`) : null,
    r.maintenance ? el("span", { class: "ok" }, r.maintenance) : null,
    el("span", {}, r.licence),
    el("span", { class: r.self_hostable_ok ? "ok" : null }, r.self_hostable)
  );
  return el(
    "li",
    { class: "rec" },
    el(
      "div",
      { class: "rec-head" },
      el("span", { class: "rec-name" }, link(r.github_url, r.name, `${r.name} on GitHub`)),
      r.fit !== null ? el("span", { class: "fit", title: "Replacement fit, 0-100" }, `Fit ${r.fit}`) : null
    ),
    facts,
    r.note ? el("p", { class: "note" }, r.note, r.note_derived ? el("span", { class: "tag" }, " (auto-summary)") : null) : null
  );
}

function render(view, onRetry) {
  main.replaceChildren();
  actions.replaceChildren();
  actions.hidden = true;
  for (const n of view.notices || []) main.append(el("p", { class: "notice", role: "status" }, n));

  if (view.kind === "loading") {
    main.append(el("p", { class: "state" }, "Loading…"));
  } else if (view.kind === "error") {
    const btn = el("button", { type: "button" }, "Try again");
    btn.addEventListener("click", onRetry);
    main.append(el("p", { class: "state" }, view.message), btn);
  } else if (view.kind === "no_match") {
    main.append(
      el("p", { class: "state" }, view.message),
      el("p", { class: "state" }, "Tracked products: ", el("strong", {}, view.supported.join(", ")), ".")
    );
  } else {
    subtitle.textContent = `Alternatives to ${view.product}`;
    if (view.kind === "no_recommendations") {
      main.append(el("p", { class: "state" }, view.message));
    } else {
      if (view.short_list) main.append(el("p", { class: "notice" }, `Only ${view.recs.length} credible alternatives right now.`));
      main.append(el("ol", { class: "recs" }, ...view.recs.map(renderRec)));
    }
    if (view.compare_url) {
      actions.append(link(view.compare_url, `Compare ${view.product} alternatives →`));
      actions.hidden = false;
    }
  }

  footer.replaceChildren(...(view.footer || []).map((t) => el("p", {}, t)));
}

async function show({ refreshing = false } = {}) {
  const [{ surface, status }, url] = await Promise.all([loadCached(chrome.storage.local), activeTabUrl()]);
  render(buildView({ surface, status, url, refreshing }), retry);
  return surface;
}

async function retry() {
  render(buildView({ surface: null, refreshing: true }), retry);
  await chrome.runtime.sendMessage({ type: "refresh" }).catch(() => null);
  await show();
}

show({ refreshing: true }).then(async (surface) => {
  // First run, or storage cleared: fetch now instead of showing an empty panel.
  if (!surface) {
    await chrome.runtime.sendMessage({ type: "refresh" }).catch(() => null);
    await show();
  }
});
