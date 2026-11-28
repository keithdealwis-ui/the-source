# The Source — Open-source discovery, alternatives and momentum

**25,000+ software projects. See what’s emerging, what’s surging and what could replace your stack.**

The Source helps people and AI agents discover software, follow projects gaining momentum and find open-source alternatives to paid tools. Explore a catalogue of 25,000+ public software projects, with deeper assessments of selected projects covering replacement fit, project health, licensing and maintenance.

Browse the website, ask your AI assistant through MCP, or build with the published data.

**[Explore alternatives](https://the-source.dev/#explore) · [See what’s moving](https://the-source.dev/#radar) · [Connect your AI assistant](https://the-source.dev/agents/#setup)**

## What will you discover?

### Spot what’s breaking out

Find emerging projects gathering attention. Explore measured growth and community conversations to decide which projects deserve a closer look.

**Ask your agent:** “Which projects under 10,000 stars are gaining momentum this week? Show the measurement dates and the conversations behind the signal.”

### Follow what’s surging

Compare growth over available measurement windows and see where the pace is accelerating. Distinguish the projects adding the most stars from those growing fastest relative to their starting point.

**[Explore the momentum radar](https://the-source.dev/#radar)** — fastest growth, stars gained and community buzz.

### Find your next software swap

Start with a paid tool such as Notion, Slack, Airtable or Dropbox. Find potential open-source replacements and compare their fit, maintenance, licensing and documented self-hosting support.

**Ask your agent:** “What could replace Notion for a self-hosted knowledge base? Compare the strongest candidates and flag migration questions I still need to investigate.”

**[Find alternatives to your software](https://the-source.dev/#explore)**

### Choose between the contenders

Put two to five projects side by side. Compare the evidence for the things that matter to you, and see where information is missing.

**Ask your agent:** “Compare Docmost and AppFlowy on licensing, project health and replacement fit. What does the evidence establish, and what would we need to test?”

### Check a project before you depend on it

Inspect maintenance, releases, licensing and evidence dates. Understand whether a project has a full assessment, a quick score or only discovery information.

**Ask your agent:** “Check this repository’s maintenance and licence. Show when the facts were observed and explain any warnings.”

### Discover tools for what you’re building

Search by a need, category or project name. Explore options for a new workflow, then narrow the shortlist using supported constraints such as licence and self-hosting.

**Ask your agent:** “Find options for a self-hosted team knowledge base. Separate recommended alternatives from discovery candidates and explain any requirements you cannot assess.”

[Connect your AI assistant to try these questions](https://the-source.dev/agents/#setup). Emerging and surging describe measured attention, not a prediction of success. A potential swap is a candidate to evaluate, not an automatic migration or a guarantee of feature parity.

## Explore broadly. Assess in depth.

The catalogue has several levels of evidence. Finding a project is the beginning of an assessment, not its conclusion.

<!-- source-stats:start -->
| Published coverage | What it means |
|---|---|
| **25,327 discovered projects** | The broad discovery corpus: public project records with evidence dates and licensing information or warnings. |
| **675 projects in the assessed layer** | Projects with the fuller assessment of health and replacement fit where applicable. |
| **1,278 recommended product–project pairings** | Specific relationships that pass the recommendation rules. One project can replace more than one product. |
| **380 products with published alternative records** | Product coverage in the read API; the number of alternatives varies. |

Assessment snapshot: **28 November 2026**. Discovery snapshot: **28 November 2026**. Individual observation dates can differ.
[Discovery counts](data/corpus/MANIFEST.json) · [Assessment and recommendation counts](data/canonical/MANIFEST.json)
<!-- source-stats:end -->

The discovery corpus and assessed layer overlap; these figures should not be added together. Discovery entries may have lightweight automated health scores. Those quick scores do not make a project a recommendation. Some entries have unresolved licences or other warnings, and these remain visible.

## Three ways to use The Source

### Explore on the website

[Visit the-source.dev](https://the-source.dev/) to find alternatives, compare their evidence and follow the momentum radar. Start with a familiar product, or explore what is gaining attention.

### Ask your AI assistant

Connect an MCP-compatible assistant to the same published evidence. Ask it to spot emerging projects, inspect a surge, find software swaps, compare a shortlist or check a project's health.

**Hosted MCP endpoint**

```text
https://mcp.the-source.dev/mcp
```

Public, read-only access over Streamable HTTP. No Source account or API key required. Client availability and workspace permissions vary; requests are rate-limited.

**Claude Code**

```bash
claude mcp add --transport http the-source https://mcp.the-source.dev/mcp
```

**Codex CLI**

```bash
codex mcp add the-source --url https://mcp.the-source.dev/mcp
```

Then try:

> Find credible open-source alternatives to Notion. Compare their licences, project health and replacement fit. Cite the evidence dates and explain anything the data cannot assess.

[Set up Claude, ChatGPT, Cursor or Codex](https://the-source.dev/agents/#setup) · [MCP connection and tool guide](docs/MCP.md)

### Build with the open data

Use the published JSON to power a comparison tool, research workflow or integration. Product records include recommendations, scores, licensing disclosures, sources and evidence dates.

| Resource | Start here |
|---|---|
| Product alternatives | [`api/v1/`](api/v1/) and the [read API guide](docs/READ-API.md) |
| Assessed projects and relationships | [`data/canonical/`](data/canonical/) |
| Broad project discovery | [`data/corpus/`](data/corpus/) and the [corpus guide](docs/CORPUS.md) |
| Repository growth and community discussion | [Momentum](docs/MOMENTUM.md) and [Community Radar](docs/RADAR.md) |

The website and hosted MCP service require no local installation. Developers can also run the MCP server from a checkout: see [the MCP guide](docs/MCP.md#run-it-yourself).

## Understand the evidence

**Replacement Fit** asks how well a project could replace a particular product. **Project Health** describes maintenance and other project signals. **Momentum** shows attention and growth over a measured period. These are separate measures.

Recommendations pass checks for project identity, maintenance, licensing and replacement evidence. Each carries its sources and evidence dates. Open-core and source-available projects are identified with their licensing disclosures.

Assessments are machine-derived from public evidence. They are not hands-on product reviews or a guarantee of security, suitability or a painless migration. Check the project's own documentation before making a commitment.

[How recommendations work](docs/METHODOLOGY.md) · [How scoring works](docs/SCORING.md) · [Trace the sources](PROVENANCE.md)

## How fresh is the data?

A scheduled daily cycle refreshes the recommendation layer and publishes measured signals. The broader discovery corpus has a separate refresh schedule. Individual records carry their own observation dates, and unavailable or stale evidence is reported.

Use the manifests or the MCP tool `source_dataset_info` to check the snapshot you are using. A newly published dataset does not mean every project was checked at the same moment.

## Contribute or get help

Found a useful project, an incorrect fact or a comparison that needs attention? [Read the contribution guide](CONTRIBUTING.md). Include the project, the affected product or result, and the supporting evidence.

[Get support](https://the-source.dev/support/) · [Report an issue](https://github.com/keithdealwis-ui/the-source/issues) · [Browse the documentation](docs/README.md)

The repository also includes a [Chrome extension](extension/README.md) for showing alternatives on supported SaaS sites. See its own guide for installation and release status.

## Licence and attribution

Code is [MIT licensed](LICENSE). Published data is generally CC BY 4.0, with share-alike terms applying to specified upstream-derived material. See [the data licence](DATA-LICENCE.md) and [attribution notices](NOTICE.md) for the scope and conditions.

Created by [Keith De Alwis](https://de-alwis.com/).

**[Find your next alternative](https://the-source.dev/#explore) · [Give your AI the evidence](https://the-source.dev/agents/#setup)**
