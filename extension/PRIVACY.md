# The Source: Open Source Alternatives — Privacy Policy

Effective 2026-09-30. Applies to version 0.1.0 and later of the Chrome extension
"The Source: Open Source Alternatives".

## Summary

The extension collects nothing about you. It has no account, no analytics, no
telemetry and no server of its own. It never reads or records your browsing history.

## How detection works

The extension downloads a public list of SaaS website addresses and hands it to
Chrome as a set of rules (`declarativeContent`). Chrome compares the page you are on
against those rules itself. The extension is not told which pages you visit; it only
learns the address of the current tab when you click its toolbar icon, and then only
to pick the matching product. That address is not stored or sent anywhere.

## What the extension downloads

Every six hours, and when you press "Try again", the extension downloads the public
read surface of The Source from GitHub
(`raw.githubusercontent.com/keithdealwis-ui/the-source`), falling back to the jsDelivr
CDN mirror of the same files (`cdn.jsdelivr.net`). It downloads every file, every
time, so the requests are identical whatever you browse. The requests carry no
cookies, no referrer and no identifier. GitHub and jsDelivr see an ordinary request
from your IP address, as for any web page; their own privacy policies apply to that.

## What is stored on your device

A copy of the downloaded public data and the time of the last download, in the
extension's local storage (`storage`). Nothing personal is stored. Removing the
extension deletes it.

## Permissions

| Permission | Why |
|---|---|
| `declarativeContent` | Lets Chrome, not the extension, recognise supported SaaS sites and show the recommendation count on the toolbar icon. |
| `activeTab` | When you click the icon, lets the extension read the current tab's address once, to choose which product's alternatives to show. |
| `storage` | Keeps the downloaded public data so the panel works offline and loads instantly. |
| `alarms` | Schedules the six-hourly refresh of the public data. |

The extension requests no host permissions, injects no scripts into pages and cannot
read page content.

## Analytics

None. If privacy-safe, aggregate analytics are ever added, they will be off until you
opt in, will never include page addresses, and this policy will be updated first.

## Contact

Keith de Alwis — issues at https://github.com/keithdealwis-ui/the-source/issues
