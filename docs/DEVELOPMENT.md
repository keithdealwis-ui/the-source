# Develop The Source locally

You can use [the website](https://the-source.dev/) or [hosted MCP service](MCP.md) without installing the pipeline. This guide is for contributors working on the repository.

## Set up a checkout

Use Python 3.12 and Git.

```bash
git clone https://github.com/keithdealwis-ui/the-source.git
cd the-source
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Check the published data

```bash
.venv/bin/python -m source_pipeline canonical-validate
.venv/bin/python -m source_pipeline.mcp_server --check
```

## Build and test a change

The existing contributor workflow rebuilds from committed inputs before checking the canonical output:

```bash
.venv/bin/python -m source_pipeline build
.venv/bin/python -m source_pipeline canonical-build
.venv/bin/python -m pytest tests/test_canonical.py -q
```

These build commands can update generated data in your checkout. Review the resulting changes. Include regenerated outputs when your source or rule change requires them, and run the tests relevant to the affected behaviour.

See [Contributing](../CONTRIBUTING.md) for reporting a problem or proposing a source, [Schema](SCHEMA.md) for data contracts and [Operations](OPERATIONS.md) for networked refreshes and deployment.

## Run the local MCP server

```bash
.venv/bin/python -m source_pipeline mcp-serve
```

This starts the stdio server using your checkout's published data. [MCP configuration and protocol reference](MCP-REFERENCE.md) covers client configuration and HTTP hosting.

## Work on the Chrome extension

The extension has its own [development and packaging instructions](../extension/README.md). Its installation and release status are separate from the hosted website and MCP service.

## Check documentation

Run `python scripts/check_docs.py` for current local links and heading anchors, and `python scripts/refresh_readme_stats.py --check` for the dated coverage block. The existing daily publication workflow refreshes that block from the published manifests after a successful cycle.

`docs/GRAPH.md` is generated: edit `docs/GRAPH.head.md`, then run `python scripts/graph_docs.py`. The intake policy and attribution notices are operational or licensed inputs, not general marketing copy.
