# Community Radar source availability

Community Radar's active public coverage is Hacker News, Forem and Bluesky. The [radar methodology](RADAR.md) explains how mentions are resolved, ranked and attributed.

The implementation also includes adapters for X and Instagram. Their presence in the code does not mean they are configured or collecting live results. Each requires appropriate provider access and configuration. Inspect `source_status` in the served dataset or the radar manifest before making a coverage claim.

Provider access rules, quotas and prices can change. Maintainers should verify the current provider documentation before enabling an adapter, and preserve the source's terms and storage restrictions.

The [October 2026 feasibility review](archive/decisions/RADAR-SOCIAL.md) is retained as dated engineering evidence, including its source links and limitations.

[See current community buzz](https://the-source.dev/#radar) · [Connect through MCP](MCP.md)
