// Renders the extension's PNG icons from inline SVG with headless Chromium.
//   icons/icon-<size>.png        brand icon (store listing, extensions page, popup header)
//   icons/idle-<size>.png        quiet toolbar icon on pages with nothing to show
//   icons/badge-<n>-<size>.png   toolbar icon on a supported product: seedling + count
// Run: npm run icons  (needs devDependencies; output is committed)

import { chromium } from "playwright-core";
import { writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { launchOptions } from "./browser.mjs";

const out = (name) => fileURLToPath(new URL(`../icons/${name}`, import.meta.url));

const seedling = (color) => `
  <path d="M16 28 V15" stroke="${color}" stroke-width="2.6" stroke-linecap="round" fill="none"/>
  <path d="M16 17 C16 10 11 6 4.5 6 C4.5 12.5 9 17 16 17 Z" fill="${color}"/>
  <path d="M16 14 C16 7.5 20.5 3.5 27.5 3.5 C27.5 10 23 14 16 14 Z" fill="${color}"/>`;

function brand() {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
    <rect x="0" y="0" width="32" height="32" rx="7" fill="#1f7a4d"/>
    <g transform="translate(3.2 3.6) scale(0.8)">${seedling("#ffffff")}</g></svg>`;
}

function idle() {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">${seedling("#7b8490")}</svg>`;
}

function badge(n) {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
    <g transform="translate(-2 -1) scale(0.82)">${seedling("#1f7a4d")}</g>
    <rect x="15" y="14" width="17" height="18" rx="5" fill="#1f7a4d" stroke="#ffffff" stroke-width="1.5"/>
    <text x="23.5" y="28" text-anchor="middle" font-family="Helvetica, Arial, sans-serif"
          font-weight="700" font-size="15" fill="#ffffff">${n}</text></svg>`;
}

const jobs = [];
for (const s of [16, 32, 48, 128]) jobs.push([`icon-${s}.png`, brand(), s]);
for (const s of [16, 32]) jobs.push([`idle-${s}.png`, idle(), s]);
for (let n = 1; n <= 5; n++) for (const s of [16, 32]) jobs.push([`badge-${n}-${s}.png`, badge(n), s]);

const browser = await chromium.launch(launchOptions());
const page = await browser.newPage({ deviceScaleFactor: 1 });
for (const [name, svg, size] of jobs) {
  await page.setViewportSize({ width: size, height: size });
  await page.setContent(
    `<html><body style="margin:0;background:transparent"><img style="display:block;width:${size}px;height:${size}px"
      src="data:image/svg+xml;base64,${Buffer.from(svg).toString("base64")}"></body></html>`
  );
  writeFileSync(out(name), await page.screenshot({ omitBackground: true, clip: { x: 0, y: 0, width: size, height: size } }));
}
await browser.close();
console.log(`wrote ${jobs.length} icons`);
