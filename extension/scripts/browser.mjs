// Chromium for the dev scripts. Set CHROMIUM_PATH to use a specific binary; otherwise
// playwright-core's own resolution applies (run `npx playwright-core install chromium`).
export function launchOptions(extra = {}) {
  const opts = { headless: true, ...extra };
  if (process.env.CHROMIUM_PATH) opts.executablePath = process.env.CHROMIUM_PATH;
  return opts;
}
