# Chrome Web Store listing — The Source: Open Source Alternatives 0.1.0

Everything the Developer Dashboard asks for, ready to paste. Submission is Keith's call
(it publishes under his developer account); this file only makes it a paste job.

**Package:** `npm run package` → `dist/the-source-extension-0.1.0.zip`
**Visibility for beta:** Unlisted (or Private to a tester group), not Public.

## Store listing

**Name:** The Source: Open Source Alternatives

**Summary (≤132):** Credible open-source alternatives to the SaaS product you are using. Detection runs in your browser; no browsing data is collected.

**Category:** Developer Tools (alternative: Productivity)
**Language:** English (UK)

**Description:**

> Using Notion, Trello, Datadog, Auth0 or another SaaS product and wondering whether
> there's an open-source option you could run yourself? The Source tells you, quietly,
> on the site you're already on.
>
> When you visit a supported product, the toolbar icon shows a seedling and the number
> of credible open-source alternatives. Click it for 3–5 of them, ranked by how well
> they replace the product. Each one shows:
>
> • Replacement fit (0–100)
> • GitHub stars and a link to the repository
> • Maintenance status: only projects active in the last 12 months are listed
> • Licence
> • Whether it can be self-hosted
> • A short summary of where it's strongest and where it falls short
>
> "Compare" opens a fuller comparison on de-alwis.com.
>
> Private by design. Chrome recognises supported sites itself, so the extension never
> sees the pages you visit. It has no host permissions, reads no page content, and has
> no account, analytics or tracking. Recommendations come from The Source, an open
> dataset on GitHub, refreshed weekly and checked against each project's repository.
>
> 20 products in this beta, including Google Analytics, Datadog, Notion, Auth0, Trello,
> Firebase, Postman, Contentful, Heroku, Shopify, Amazon S3, New Relic, Jira, Asana,
> Zoom, Dropbox, Google Drive, Monday.com and ChatGPT.

**Graphic assets**

| Asset | File |
|---|---|
| Store icon 128×128 | `icons/icon-128.png` |
| Screenshot 1 (1280×800) | `store/screenshot-1.png` |
| Screenshot 2 (1280×800) | `store/screenshot-2.png` |
| Small promo tile (440×280) | `store/promo-small.png` |

**Official URL / homepage:** https://github.com/keithdealwis-ui/the-source
**Support URL:** https://github.com/keithdealwis-ui/the-source/issues

## Privacy practices tab

**Single purpose:** Show open-source alternatives to the SaaS product in the current tab.

**Permission justifications**

| Permission | Justification |
|---|---|
| `declarativeContent` | Lets Chrome recognise supported SaaS sites from a published list and change the toolbar icon to show the number of alternatives, without the extension reading any page address. |
| `activeTab` | When the user clicks the toolbar icon, reads the current tab's address once to choose which product's alternatives to show. Not stored or transmitted. |
| `storage` | Caches the public recommendation dataset so the panel opens instantly and works offline. |
| `alarms` | Refreshes the public dataset every six hours. |

**Remote code:** No. All code ships in the package; the extension downloads JSON data only.

**Data usage** — tick **none** of the data categories (personally identifiable
information, health, financial, authentication, personal communications, location,
web history, user activity, website content). Then certify all three:

- I do not sell or transfer user data to third parties, outside of the approved use cases
- I do not use or transfer user data for purposes that are unrelated to my item's single purpose
- I do not use or transfer user data to determine creditworthiness or for lending purposes

**Privacy policy URL:** https://github.com/keithdealwis-ui/the-source/blob/main/extension/PRIVACY.md

## Before submitting (Keith)

1. `cd extension && npm test && npm run e2e && npm run package`.
2. Load `dist/…zip` unpacked (unzip it) in your own Chrome and visit trello.com: the
   seedling icon should show a count, and clicking it should list alternatives. This is
   the one path no automated test can drive (a real toolbar click).
3. Developer Dashboard → New item → upload the zip → paste the above → Unlisted → Submit.
