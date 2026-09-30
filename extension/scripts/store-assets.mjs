// Chrome Web Store images from the real panel screenshots that `npm run e2e` captured:
//   store/screenshot-1.png  (1280x800)  panel on a supported product
//   store/screenshot-2.png  (1280x800)  privacy / how it works
//   store/promo-small.png   (440x280)   small promo tile
// Run after `npm run e2e`.
import { chromium } from "playwright-core";
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { launchOptions } from "./browser.mjs";

const u = (p) => fileURLToPath(new URL(p, import.meta.url));
const img = (p) => `data:image/png;base64,${readFileSync(u(p)).toString("base64")}`;
const panel = img("../.e2e/panel-notion.png");
const panel2 = img("../.e2e/panel-trello.png");
const icon = img("../icons/icon-128.png");
const badge = img("../icons/badge-5-32.png");

const css = `
  *{box-sizing:border-box} body{margin:0;font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:#16181d}
  .bg{width:1280px;height:800px;background:linear-gradient(135deg,#eef6f1,#dfeee6);display:flex;align-items:center;gap:64px;padding:0 90px}
  h1{font-size:46px;line-height:1.1;margin:0 0 18px;letter-spacing:-.5px} p{font-size:21px;line-height:1.45;color:#3d4450;margin:0 0 14px}
  .shot{border-radius:12px;box-shadow:0 18px 50px rgba(20,60,40,.25);background:#fff;overflow:hidden;flex:none}
  .bar{height:44px;background:#f1f3f4;display:flex;align-items:center;gap:10px;padding:0 14px;border-bottom:1px solid #dadce0}
  .url{flex:1;height:28px;border-radius:14px;background:#fff;font-size:14px;color:#5f6368;display:flex;align-items:center;padding:0 14px}
  ul{margin:8px 0 0;padding-left:22px;font-size:20px;line-height:1.7;color:#3d4450}`;

const shot1 = `<style>${css}</style><div class="bg">
  <div style="flex:1"><img src="${icon}" width="72" style="margin-bottom:22px">
    <h1>Open-source alternatives, right where you work</h1>
    <p>On a supported SaaS product, the toolbar icon shows how many credible open-source alternatives exist. Click for 3–5, ranked by fit.</p>
    <p>Each shows GitHub stars, maintenance status, licence and whether you can self-host it.</p></div>
  <div class="shot" style="width:410px"><div class="bar"><div class="url">notion.so</div><img src="${badge}" width="22"></div>
    <img src="${panel}" width="380" style="display:block;margin:0 15px"></div></div>`;

const shot2 = `<style>${css}</style><div class="bg">
  <div class="shot" style="width:410px"><div class="bar"><div class="url">trello.com</div><img src="${badge}" width="22"></div>
    <img src="${panel2}" width="380" style="display:block;margin:0 15px"></div>
  <div style="flex:1"><h1>Private by design</h1>
    <ul><li>Chrome recognises supported sites itself; the extension never sees your browsing</li>
    <li>No host permissions, no page access, no scripts injected</li>
    <li>No account, no analytics, no tracking</li>
    <li>Recommendations come from The Source, an open dataset on GitHub</li></ul></div></div>`;

const promo = `<style>${css}</style><div style="width:440px;height:280px;background:#1f7a4d;color:#fff;display:flex;flex-direction:column;justify-content:center;padding:0 36px">
  <img src="${icon}" width="56" style="margin-bottom:16px;border-radius:12px;box-shadow:0 0 0 2px rgba(255,255,255,.35)">
  <div style="font-size:30px;font-weight:700;line-height:1.1">The Source</div>
  <div style="font-size:18px;opacity:.9;margin-top:6px">Open-source alternatives to the SaaS you use</div></div>`;

const browser = await chromium.launch(launchOptions());
const page = await browser.newPage({ deviceScaleFactor: 1 });
for (const [name, html, w, h] of [["screenshot-1.png", shot1, 1280, 800], ["screenshot-2.png", shot2, 1280, 800], ["promo-small.png", promo, 440, 280]]) {
  await page.setViewportSize({ width: w, height: h });
  await page.setContent(`<html><body style="margin:0">${html}</body></html>`);
  writeFileSync(u(`../store/${name}`), await page.screenshot({ clip: { x: 0, y: 0, width: w, height: h } }));
}
await browser.close();
console.log("wrote store/screenshot-1.png, store/screenshot-2.png, store/promo-small.png");
