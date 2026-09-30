// End-to-end: load the unpacked extension in Chromium, let it fetch the LIVE read
// surface, and check detection rules, the panel on a supported site, no-match, offline
// with a saved copy, and first run with no network. Supported-site pages are served
// locally by request interception, so no SaaS site is contacted.
// Writes evidence (JSON + screenshots) to .e2e/.   Run: npm run e2e

import { chromium } from "playwright-core";
import { mkdirSync, writeFileSync, rmSync, cpSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { launchOptions } from "./browser.mjs";

const srcDir = fileURLToPath(new URL("..", import.meta.url));
const outDir = fileURLToPath(new URL("../.e2e/", import.meta.url));
rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });

// Test copy of the extension. Clicking the toolbar icon grants activeTab, and with it the
// tab's URL; a headless browser has no toolbar to click and chrome.action.openPopup()
// grants nothing. The copy therefore adds "tabs" so the popup can read the URL it would
// get from a real click. Code is byte-identical; only the manifest differs.
const extDir = `${outDir}ext`;
for (const part of ["src", "popup", "icons"]) cpSync(`${srcDir}${part}`, `${extDir}/${part}`, { recursive: true });
const testManifest = JSON.parse(readFileSync(`${srcDir}manifest.json`));
testManifest.permissions = [...testManifest.permissions, "tabs"];
writeFileSync(`${extDir}/manifest.json`, JSON.stringify(testManifest, null, 2));

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: !!ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail !== undefined ? `  ${JSON.stringify(detail)}` : ""}`);
}

async function launch(profile) {
  const ctx = await chromium.launchPersistentContext(
    profile,
    launchOptions({
      args: [`--disable-extensions-except=${extDir}`, `--load-extension=${extDir}`, `--remote-debugging-port=${PORT}`],
      viewport: { width: 1280, height: 800 },
    })
  );
  // Serve every page other than the extension's own from a local stub.
  await ctx.route(/^https?:\/\/(?!raw\.githubusercontent\.com|cdn\.jsdelivr\.net)/, (route) =>
    route.fulfill({ status: 200, contentType: "text/html", body: `<title>stub</title><h1>${route.request().url()}</h1>` })
  );
  let [sw] = ctx.serviceWorkers();
  if (!sw) sw = await ctx.waitForEvent("serviceworker");
  return { ctx, sw, id: new URL(sw.url()).host };
}

async function waitFor(fn, ms = 30000) {
  const end = Date.now() + ms;
  for (;;) {
    const v = await fn();
    if (v) return v;
    if (Date.now() > end) return v;
    await new Promise((r) => setTimeout(r, 250));
  }
}

// Playwright does not surface action popups as pages, so the popup is driven over the
// DevTools protocol directly: find its target, evaluate in it, screenshot it.
const PORT = 9337;

async function cdpTarget(pred, ms = 10000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const list = await fetch(`http://127.0.0.1:${PORT}/json/list`).then((r) => r.json()).catch(() => []);
    const t = list.find(pred);
    if (t) return t;
    await new Promise((r) => setTimeout(r, 200));
  }
  return null;
}

function cdpSession(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let seq = 0;
  const pending = new Map();
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const { resolve, reject } = pending.get(m.id);
      pending.delete(m.id);
      m.error ? reject(new Error(m.error.message)) : resolve(m.result);
    }
  };
  const ready = new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
  const send = async (method, params = {}) => {
    await ready;
    const id = ++seq;
    ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
  };
  const evaluate = async (expr) => {
    const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    return r.result.value;
  };
  return {
    evaluate,
    async waitFor(expr, ms = 30000) {
      const end = Date.now() + ms;
      while (Date.now() < end) {
        if (await evaluate(expr)) return true;
        await new Promise((r) => setTimeout(r, 200));
      }
      return false;
    },
    async screenshot(path) {
      const { data } = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      writeFileSync(path, Buffer.from(data, "base64"));
    },
    close: () => ws.close(),
  };
}

async function openPanelFor(ctx, sw, id, url) {
  const page = await ctx.newPage();
  await page.goto(url);
  await page.bringToFront();
  const opened = await sw.evaluate(() => chrome.action.openPopup().then(() => true, (e) => String(e)));
  const t = await cdpTarget((t) => t.url === `chrome-extension://${id}/popup/popup.html` && t.webSocketDebuggerUrl);
  return { page, popup: t ? cdpSession(t.webSocketDebuggerUrl) : null, opened };
}

async function panelText(popup) {
  await popup.waitFor(`!document.querySelector("#main .state")?.textContent.includes("Loading")`);
  return popup.evaluate(`(() => ({
    subtitle: document.getElementById("subtitle").textContent,
    main: document.getElementById("main").innerText,
    recs: [...document.querySelectorAll(".rec")].map((li) => ({
      name: li.querySelector(".rec-name").textContent,
      href: li.querySelector(".rec-name a").href,
      facts: li.querySelector(".facts").innerText,
      fit: li.querySelector(".fit")?.textContent,
    })),
    compare: document.querySelector("#actions a")?.href || null,
    footer: document.getElementById("footer").innerText,
    notices: [...document.querySelectorAll(".notice")].map((n) => n.textContent),
    viewport: innerHeight,
    footer_bottom: Math.round(document.getElementById("footer").getBoundingClientRect().bottom),
  }))()`);
}

async function closePopup(popup) {
  await popup.evaluate("window.close()").catch(() => null);
  popup.close();
  await new Promise((r) => setTimeout(r, 300));
}

// ---- Run 1: online, fresh profile ---------------------------------------------------
{
  const profile = `${outDir}profile-online`;
  const { ctx, sw, id } = await launch(profile);

  const surfaceOk = await waitFor(async () => {
    const s = await sw.evaluate(() => chrome.storage.local.get(["surface", "rules_version", "rules_error"]));
    return s.surface && s.rules_version ? s : null;
  });
  check("service worker fetched the live read surface", surfaceOk && Object.keys(surfaceOk.surface.products).length === 20, {
    source: surfaceOk?.surface?.source,
    dataset_version: surfaceOk?.surface?.dataset_version,
  });
  check("no rule registration error", !surfaceOk?.rules_error, surfaceOk?.rules_error);

  const rules = await sw.evaluate(() => new Promise((r) => chrome.declarativeContent.onPageChanged.getRules(undefined, r)));
  check("declarativeContent has one rule per supported product", rules.length === 20, rules.length);
  check(
    "every rule sets the toolbar icon",
    rules.every((r) => r.actions.length === 1 && r.actions[0].instanceType === "declarativeContent.SetIcon"),
    rules.slice(0, 1).map((r) => ({ id: r.id, conditions: r.conditions.length, action: r.actions[0]?.instanceType }))
  );
  writeFileSync(`${outDir}rules.json`, JSON.stringify(rules.map((r) => ({ id: r.id, conditions: r.conditions.map((c) => c.pageUrl) })), null, 2));

  const perms = await sw.evaluate(() => chrome.permissions.getAll());
  check("granted permissions: the declared four (+ tabs in the test copy only), no origins", JSON.stringify(perms) === JSON.stringify({ origins: [], permissions: ["activeTab", "alarms", "declarativeContent", "storage", "tabs"] }), perms);

  const alarms = await sw.evaluate(() => chrome.alarms.getAll());
  check("6-hourly refresh alarm scheduled", alarms.some((a) => a.name === "refresh-surface" && a.periodInMinutes === 360), alarms);

  // Supported sites, each through the real popup opened on the real tab.
  const probes = [
    ["trello", "https://trello.com/b/abc/board"],
    ["notion", "https://www.notion.so/workspace/page"],
    ["jira", "https://acme.atlassian.net/jira/software/projects"],
    ["google-analytics", "https://analytics.google.com/analytics/web/"],
    ["amazon-s3", "https://aws.amazon.com/s3/pricing/"],
  ];
  let shot = 0;
  for (const [saas, url] of probes) {
    const { page, popup, opened } = await openPanelFor(ctx, sw, id, url);
    if (!popup) {
      check(`panel opens on ${saas}`, false, { opened });
      await page.close();
      continue;
    }
    const t = await panelText(popup);
    const product = surfaceOk.surface.index.supported_saas.find((r) => r.saas_id === saas);
    check(`panel on ${saas}: 3-5 recommendations`, t.recs.length >= 3 && t.recs.length <= 5, t.recs.length);
    check(`panel on ${saas}: title names ${product.name}`, t.subtitle === `Alternatives to ${product.name}`, t.subtitle);
    check(
      `panel on ${saas}: GitHub link, stars, maintenance, licence, self-hosting, fit`,
      t.recs.every((r) => r.href.startsWith("https://github.com/") && /★ [\d.]+k?/.test(r.facts) && /(Active|Maintained)/.test(r.facts) && /Self-host/.test(r.facts) && /^Fit \d+$/.test(r.fit)),
      t.recs[0]
    );
    check(`panel on ${saas}: compare link`, t.compare === `https://de-alwis.com${product.compare_path}`, t.compare);
    check(`panel on ${saas}: compare link and footer visible without scrolling`, t.footer_bottom <= t.viewport + 2, [t.footer_bottom, t.viewport]);
    check(`panel on ${saas}: shows when data was checked`, /GitHub data checked \d{4}-\d\d-\d\d/.test(t.footer), t.footer);
    if (shot++ < 2) {
      await popup.screenshot(`${outDir}panel-${saas}.png`);
    }
    writeFileSync(`${outDir}panel-${saas}.json`, JSON.stringify(t, null, 2));
    await closePopup(popup);
    await page.close();
  }

  {
    const { page, popup, opened } = await openPanelFor(ctx, sw, id, "https://example.com/");
    if (popup) {
      const t = await panelText(popup);
      check("no-match site: says nothing is tracked", t.main.includes("No open-source alternatives tracked for this site") && t.recs.length === 0, t.main.split("\n")[0]);
      await popup.screenshot(`${outDir}panel-no-match.png`);
      await closePopup(popup);
    } else check("no-match panel opens", false, { opened });
    await page.close();
  }

  // Offline with a saved copy: refresh fails, the saved copy is shown with a notice.
  await ctx.setOffline(true);
  await ctx.route(/^https:\/\/(raw\.githubusercontent\.com|cdn\.jsdelivr\.net)/, (r) => r.abort("internetdisconnected"));
  const ext = await ctx.newPage();
  await ext.goto(`chrome-extension://${id}/popup/popup.html`);
  const failed = await ext.evaluate(() => chrome.runtime.sendMessage({ type: "refresh" }));
  const st = await sw.evaluate(() => chrome.storage.local.get("status"));
  check("offline refresh (the Try again path) fails cleanly, keeps data, records why", failed.ok === false && failed.has_data === true && !!st.status.last_error, st.status.last_error?.slice(0, 120));
  await ext.close();
  {
    const { page, popup } = await openPanelFor(ctx, sw, id, "https://trello.com/");
    if (popup) {
      const t = await panelText(popup);
      check("offline: saved copy still shown, with a notice", t.recs.length >= 3 && t.notices.some((n) => n.includes("last saved copy")), t.notices);
      await popup.screenshot(`${outDir}panel-offline-cached.png`);
      await closePopup(popup);
    } else check("offline panel opens", false);
    await page.close();
  }
  await ctx.close();
}

// ---- Run 2: first run with no network --------------------------------------------------
{
  const profile = `${outDir}profile-offline`;
  const ctx = await chromium.launchPersistentContext(
    profile,
    launchOptions({ args: [`--disable-extensions-except=${extDir}`, `--load-extension=${extDir}`, `--remote-debugging-port=${PORT}`] })
  );
  await ctx.route(/^https:\/\/(raw\.githubusercontent\.com|cdn\.jsdelivr\.net)/, (r) => r.abort("internetdisconnected"));
  await ctx.route(/^https?:\/\/(?!raw\.githubusercontent\.com|cdn\.jsdelivr\.net)/, (r) => r.fulfill({ status: 200, contentType: "text/html", body: "stub" }));
  let [sw] = ctx.serviceWorkers();
  if (!sw) sw = await ctx.waitForEvent("serviceworker");
  const id = new URL(sw.url()).host;
  await waitFor(() => sw.evaluate(() => chrome.storage.local.get("status").then((s) => s.status && s.status.last_error)), 30000);
  const { page, popup } = await openPanelFor(ctx, sw, id, "https://trello.com/");
  if (popup) {
    await popup.waitFor(`!!document.querySelector("button")`, 40000);
    const t = await panelText(popup);
    const hasRetry = await popup.evaluate(`!!document.querySelector("button")`);
    check("first run offline: error with Try again, no crash", t.main.includes("Couldn't load recommendations") && !!hasRetry, t.main);
    await popup.screenshot(`${outDir}panel-offline-first-run.png`);
  } else check("first-run offline panel opens", false);
  await page.close();
  await ctx.close();
}

rmSync(`${outDir}profile-online`, { recursive: true, force: true });
rmSync(`${outDir}profile-offline`, { recursive: true, force: true });
rmSync(extDir, { recursive: true, force: true });
const failedChecks = results.filter((r) => !r.ok);
writeFileSync(`${outDir}results.json`, JSON.stringify({ at: new Date().toISOString(), passed: results.length - failedChecks.length, failed: failedChecks.length, results }, null, 2));
console.log(`\n${results.length - failedChecks.length}/${results.length} checks passed`);
process.exit(failedChecks.length ? 1 : 0);
