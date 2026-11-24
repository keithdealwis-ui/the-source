# The Source: Open Source Alternatives — Store listing copy

Use this copy only for a tested extension release. Distribution, supported coverage and screenshots must match the uploaded package; see [release checks](../RELEASING.md).

**Name:** The Source: Open Source Alternatives

**Summary:** Find open-source alternatives to the SaaS tool you’re using. Compare fit, maintenance and licensing from your browser toolbar.

**Category:** Developer Tools

**Language:** English (UK)

## Description

Could an open-source project replace a tool you already use? The Source helps you build a shortlist while browsing supported SaaS websites.

Open the toolbar panel to see available alternatives with replacement-fit scores, repository links, maintenance information, licences and documented self-hosting support. Each result includes its evidence date. Short lists stay short: the extension does not add weaker candidates just to fill the panel.

Assessments are machine-derived from public evidence. Check the project’s documentation and test your workflow before making a switch. Preserve any open-core or source-available licence disclosure when evaluating a result.

Detection runs locally. The extension reads the current tab’s URL when you click the icon, uses it to select a product and does not store or send that URL. It has no account, analytics or tracking and downloads the public dataset on a regular schedule. See the extension privacy policy for permissions and data-provider requests.

Want to discover more? Visit https://the-source.dev/ to explore alternatives and the momentum radar, or connect your AI assistant at https://the-source.dev/agents/.

**Homepage:** https://the-source.dev/

**Support:** https://the-source.dev/support/

**Privacy:** https://github.com/keithdealwis-ui/the-source/blob/main/extension/PRIVACY.md

## Graphic assets

Use `icons/icon-128.png`, `store/screenshot-1.png`, `store/screenshot-2.png` and `store/promo-small.png` only after checking that they match the release being submitted.

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
