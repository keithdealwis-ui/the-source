# Website and MCP discovery

The canonical product website is https://the-source.dev/. The factual reference at
https://the-source.dev/ai-instructions/ and the plain-text guide at
https://the-source.dev/llms.txt link to this repository's maintained documentation.
The connection guide is https://the-source.dev/agents/.

## Registry preparation, 23 October 2026

`server.json` describes the existing public remote endpoint. It does not deploy or
change the server. Version 1.0.0 is the proposed initial registry metadata version,
not a claim about the server implementation's version.

Publication is not confirmed. The official registry API timed out during checks.
Before publishing, the backend owner should:

1. Search the official registry for `io.github.keithdealwis-ui/the-source` and the
   existing endpoint. Reuse an existing entry and its version history if present.
2. Review the metadata and confirm it matches the deployed service.
3. Authenticate the verified GitHub namespace with `mcp-publisher login github`.
4. Run `mcp-publisher validate`, then `mcp-publisher publish` if no conflicting
   listing/version exists. Never overwrite or duplicate a listing blindly.
5. Read the published entry back from the registry and record its URL/version.

Official guide: https://modelcontextprotocol.io/registry/quickstart

Do not add API keys to the listing: this endpoint is public and read-only.
Listing metadata is not automatic client installation or a guarantee of discovery.
No pipeline, scoring, endpoint, authentication or deployment changes are included.
