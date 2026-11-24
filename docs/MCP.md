# The Source MCP server — Discover software, find alternatives and follow momentum

Give your assistant access to The Source's published software intelligence. Spot emerging projects, follow surging momentum, find alternatives to paid tools, compare a shortlist and inspect project health without leaving your conversation.

The hosted server is public and read-only. It requires no Source account or API key.

## Connect

```text
https://mcp.the-source.dev/mcp
```

Transport: **Streamable HTTP**. Use a client that supports remote MCP servers. Availability depends on the client, account and workspace permissions.

### Claude Code

```bash
claude mcp add --transport http the-source https://mcp.the-source.dev/mcp
```

Run `/mcp` in Claude Code to check the connection. [Official instructions](https://code.claude.com/docs/en/mcp).

### Codex CLI

```bash
codex mcp add the-source --url https://mcp.the-source.dev/mcp
codex mcp list
```

[Official instructions](https://developers.openai.com/codex/mcp/).

### Cursor

Merge this entry into your MCP configuration, preserving any existing servers:

```json
{
  "mcpServers": {
    "the-source": {
      "url": "https://mcp.the-source.dev/mcp"
    }
  }
}
```

Check that The Source and its tools appear in Cursor's MCP settings. [Official instructions](https://cursor.com/docs/mcp).

### Claude and ChatGPT

Where your account supports custom remote MCP connections, add **The Source** using the endpoint above. Choose no authentication if prompted, then enable the connection for your conversation.

[Client-specific setup](https://the-source.dev/agents/#setup) includes the website's current instructions for Claude, ChatGPT, Cursor and Codex. If the option is missing, check your account's connector availability and workspace permissions.

## Try your first question

> Find credible open-source alternatives to Notion. Compare their licences, project health and replacement fit. Cite the evidence dates and explain anything the data cannot assess.

Then narrow the question:

> Which of those have documented self-hosting support? Compare the strongest two and show the evidence behind their scores.

## More ways to use it

| Use case | Example question |
|---|---|
| Spot a breakout | “Which projects under 10,000 stars are gaining momentum this week? Show the evidence dates and community sources.” |
| Investigate a surge | “Is this project's star growth accelerating? Compare the available windows and show where history is insufficient.” |
| Find a software swap | “Find self-hosted alternatives to Notion. Compare replacement fit and licences, and flag migration questions to investigate.” |
| Choose a contender | “Compare Docmost and AppFlowy. Which differences are supported by evidence, and which need hands-on testing?” |
| Check a dependency | “Inspect this repository's maintenance, releases and licensing. How fresh is the evidence?” |
| Find tools for a new workflow | “Find software for a self-hosted team knowledge base. Separate recommendations from discovery candidates.” |

These are prompts for live queries, not precomputed results. Growth and community attention do not predict success; software swaps still require evaluation and migration planning.

Ask your assistant to check `source_dataset_info` when freshness matters. A successful answer should identify its evidence dates and preserve any warnings or missing information.

## Tools

| Task | MCP tool |
|---|---|
| Find alternatives to a paid product | `source_get_alternatives` |
| Match requirements and constraints | `source_recommend` |
| Compare two to five projects | `source_compare_projects` |
| Search products and projects | `source_search` |
| Inspect a project's facts and evidence | `source_get_project` |
| Explain a product–project pairing | `source_get_relationship` |
| Find emerging projects | `source_emerging_projects` |
| Inspect growth and community signals | `source_get_momentum` |
| Check product coverage | `source_list_supported_saas` |
| Match a URL to a known SaaS product | `source_detect_saas` |
| Check the dataset, dates and coverage | `source_dataset_info` |

The URL detection tool matches published rules; it does not visit the supplied URL. The server reads published data and cannot install software or submit projects.

## Read the results correctly

Recommended, scored and discovery are different states. Discovery projects may have quick scores, but these are not the full assessment and cannot qualify a project for recommendation.

Replacement Fit concerns a specific product–project pairing. Project Health describes the project. Repository and community momentum describe attention. Keep them separate and preserve licensing disclosures, evidence dates, unknown values and requirements marked `not_evaluated`.

Responses are compact by default. Use `detail: "full"` or request named blocks with `include` when you need more supporting detail. See the [tool and protocol reference](MCP-REFERENCE.md) for arguments, response fields, verification and errors.

## Troubleshooting

| Problem | What to check |
|---|---|
| No tools appear | Use the full `/mcp` URL with Streamable HTTP, enable the connection and refresh the client's tool list. |
| The connection option is unavailable | Check the client's remote-MCP support, account limits and workspace permissions. |
| A request is rate-limited | Pause before retrying and reduce repeated requests. |
| Data is stale or unavailable | Read `source_dataset_info` and the relevant record timestamps. The last usable snapshot may still be served with a warning. |
| No recommendation matches | Check product coverage and your constraints. A discovery result is not a substitute for a missing recommendation. |

[Get support](https://the-source.dev/support/) with the client name, version, tool and error message. Remove private information before sharing.

## Run it yourself

For developers who want a local server, clone the repository and follow the [developer setup](DEVELOPMENT.md).

```bash
.venv/bin/python -m source_pipeline.mcp_server --check
.venv/bin/python -m source_pipeline mcp-serve
```

The second command starts the stdio server over the checkout's published data. Local data reflects your checkout; it does not automatically become the latest hosted snapshot. See the [reference](MCP-REFERENCE.md) for stdio client configuration and local HTTP hosting.

## Privacy and attribution

The hosted service uses aggregate operational counts and latency measurements. The documented telemetry does not retain tool arguments, results or client addresses. See the [privacy notice](https://the-source.dev/privacy/) and technical reference for scope and retention.

Results include dataset metadata and attribution. Retain the applicable licence disclosures and notices when reusing them. See [data licensing](../DATA-LICENCE.md).

Prefer to browse? **[Explore The Source](https://the-source.dev/)**.

## Technical reference

<a id="what-it-reads"></a>
<a id="guarantees"></a>
<a id="response-detail-kei-961"></a>
<a id="retrieval"></a>
<a id="errors"></a>
<a id="protocol"></a>

The detailed [read-layer and verification contract](MCP-REFERENCE.md#what-it-reads), [response detail](MCP-REFERENCE.md#response-detail), [retrieval](MCP-REFERENCE.md#retrieval), [errors](MCP-REFERENCE.md#errors) and [protocol](MCP-REFERENCE.md#protocol) are maintained separately from this setup guide.
