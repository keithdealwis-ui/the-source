# Release the Chrome extension

The extension's store release is separate from the website and hosted MCP service. This guide is for maintainers, not an installation announcement.

1. Run `npm test`, `npm run e2e` and `npm run package` from `extension/` using the documented Node/Chromium dependencies.
2. Load the packaged code unpacked in Chrome. Check a supported website, a non-matching site, short lists, stale data and offline recovery.
3. Check the Compare destination. The current code uses de-alwis.com legacy comparison routes; do not advertise a new destination until the implementation and published pages support it.
4. Review displayed licence disclosures against current recommendations. Store copy, screenshots and the privacy policy must match the actual shipped behaviour.
5. Confirm the package version and the intended public, unlisted or private distribution. Upload it through the developer account and review the store's submission requirements.
6. After approval, record the actual install URL and version in the extension README before promoting an Install button elsewhere.

[Listing copy and permission explanations](store/LISTING.md) · [Extension privacy](PRIVACY.md)
