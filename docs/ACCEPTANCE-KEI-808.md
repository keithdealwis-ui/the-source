# KEI-808 acceptance — The Source Chrome extension MVP

Evidence map for the eight acceptance criteria. Code is in `extension/`. Unit tests:
`cd extension && npm test` (29 tests, run in CI as job `extension`). End to end:
`npm run e2e` loads the unpacked extension in Chromium against the live read surface
(40 checks). Mutation control: adding a `tabs` permission, dropping `www.` stripping,
dropping path-prefix precedence, sending credentials, and removing path validation each
turned the unit suite red; restoring made it green.

| # | Criterion | Evidence | Status |
|---|---|---|---|
| 1 | Detects all initial supported SaaS domains | `test/match.test.mjs`: each of the 44 rules in `api/v1/domains.json` detects its product, and every one of the 20 products is covered. The JS matcher agrees with the Python reference (`tests/test_canonical.py`) on every URL of a 300+ URL corpus. The Chrome-side `declarativeContent` filters light up exactly the same URLs, and never two products at once. E2E: 20 rules registered in Chrome, one per product, each with a SetIcon action; panels open correctly on trello, notion (www.), jira (subdomain + path), google-analytics and amazon-s3 (path). | Met |
| 2 | Shows 3–5 canonical recommendations when available | `src/view.js` shows at most 5, and fewer only when the API has fewer. It never pads, and a short list is labelled. Unit test: all 20 products produce 3–5. E2E: 3–5 on each of 5 live products. | Met |
| 3 | GitHub URL/stars + active-maintenance status | Each row links to the repository (only `https://github.com/owner/repo` links are rendered) and shows ★ stars, Active/Maintained, licence, self-hosting, Fit and the auto-summary note. Checked for every product (unit) and on the live panel (E2E). | Met |
| 4 | Uses canonical read surface, not bundled data | The package holds code and icons only (`test/manifest.test.mjs`: no data JSON, no embedded product data). It fetches `raw.githubusercontent.com/keithdealwis-ui/the-source/main/api/v1/`, falling back to the jsDelivr mirror. A snapshot is accepted only if all files share one `dataset_version`. E2E: fetched live, `dataset_version` 4a669ecae22a2c92. New products need no extension release. | Met |
| 5 | Handles no-match / offline / error gracefully | Unit and E2E: a non-supported site shows "No open-source alternatives tracked" plus the product list. Offline with a saved copy shows the copy with a notice. Offline on first run shows an error with "Try again". HTTP errors and mixed versions never replace the cache. Data older than 14 days is flagged. A short list is labelled. A product with none gets a message. | Met |
| 6 | Permissions minimal and documented | `declarativeContent`, `activeTab`, `storage`, `alarms`: none of them triggers an install warning. There are no host permissions, content scripts, `tabs`, `history` or `webNavigation`, and the CSP `connect-src` is limited to the two read-surface origins. Documented in `extension/README.md` and `extension/PRIVACY.md`, and enforced by `test/manifest.test.mjs`. E2E: `chrome.permissions.getAll()` shows exactly these (the E2E copy adds `tabs` only to stand in for a real toolbar click). | Met |
| 7 | No browsing-history collection | Chrome matches the pages, so the extension is never told the URL. It reads the tab URL only on an icon click (`activeTab`), and neither stores nor sends it. Downloads are the whole surface on a timer, so requests are identical whatever the user browses (unit-tested), with no cookies or referrer. No analytics of any kind (lint-tested). | Met |
| 8 | Ready for Chrome Web Store beta submission | `npm run package` builds `dist/the-source-extension-0.1.0.zip` (24 files, 22.7 KB, contents checked against the manifest). `extension/store/LISTING.md` has the listing copy, category, permission justifications, data-use answers and privacy-policy URL. The 1280×800 screenshots and 440×280 promo tile come from real panel captures; the 128 px icon is included. Submission itself is Keith-class (his developer account, external publication). | Met (submission is Keith's) |

## Residue (non-blocking)

- **Real toolbar click not automated.** Headless Chromium has no toolbar, and
  `chrome.action.openPopup()` grants no `activeTab`. So the E2E copy adds `tabs` to
  stand in for the click; the code is byte-identical. The rest of the path (rules
  registered, icon action, panel) is proven. Step 2 of `store/LISTING.md` is the manual
  check.
- **Compare pages 404 until KEI-809 ships.** The link is built from the API's
  `compare_path` (`de-alwis.com/open-source/<product>-alternatives`). KEI-809 (Defined)
  builds those pages.
- **`www.<suffix root>` edge case.** For example, `www.notion.site` lights the icon but
  the panel treats it as not a product, because Chrome matches the raw hostname. It is
  not a real product URL.
