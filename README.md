# CityEngine Cloud MCP Server & Tools

<!-- mcp-name: io.github.cgajs/cityengine-mcp -->

Text-to-3D city buildings in one sentence: natural language → CGA rules → cloud engine → glTF/glb — plus demo browsing and quota lookup. Read-only compute endpoints only.

This repository contains two complementary packages:

| Package | What it is | Use it with |
|---|---|---|
| `cityengine-mcp` (repo root) | [MCP](https://modelcontextprotocol.io) server (stdio, FastMCP) | kimi code, Codex CLI, Claude Desktop, any MCP client |
| `cityengine-tools/` | OpenAI-compatible function-calling toolkit (schemas + client) | DeepSeek, Qwen, GLM, GPT and any OpenAI-style tool-calling API |

Live demos and self-service API keys: **https://www.cityengine.cn/plugins.html** · API contract: https://api.cityengine.cn

## Tools (6)

| Tool | Description | Quota |
|---|---|---|
| `generate_cga` | Natural language → CGA rule source (sync, no rendering); supports `base_cga + instruction` refinement | NL quota |
| `parse_cga` | CGA source + attributes → engine parse → 3D model stats + glb download (sha256 verified) | generate quota |
| `generate_model` | Natural language → ready 3D model (NL2CGA + parse chained), returns `cga_source` too | NL + generate |
| `list_demos` | List showcase demos on the official site (id/name/engine/attribute count) | free |
| `get_demo` | Inspect a demo's attribute panel, node/material counts, engine timing | free |
| `get_quota` | Current key's plan and quota usage | free |

All endpoints are read-only compute calls — nothing writes to your account or the site.

## Install (MCP server)

```bash
pip install cityengine-mcp        # or: pip install .  from this repo
# provides the `cityengine-mcp` console script; or run: python -m cityengine_mcp.server
```

Requirements: Python ≥ 3.10, `mcp` 1.x, `httpx`.

## Get an API key

Keys use the `cvk_` prefix. Two self-service ways:

1. Website: register/log in at <https://www.cityengine.cn> → User Center → "插件 Key" → issue (shown once, save it).
2. CLI:

```bash
curl -X POST https://api.cityengine.cn/v1/register \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"strong-password","nickname":"optional"}'
# the response api_key is your plugin key (shown in full only once)
```

Free tier: 5 NL generations/month, 100 model generations/day. Paid members: unlimited NL.

## Configure your MCP client

Environment variables:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `CITYENGINE_API_KEY` | yes | — | plugin API key (`cvk_…`) |
| `CITYENGINE_GATEWAY_BASE` | no | `https://api.cityengine.cn` | gateway base URL |
| `CITYENGINE_DEMOS_BASE` | no | `https://www.cityengine.cn` | demo listing site |
| `CITYENGINE_OUTPUT_DIR` | no | `./cityengine_mcp_output` | where glb files land |
| `CITYENGINE_MAX_POLL_S` | no | `300` | render job poll cap (seconds) |

Generic MCP client config:

```json
{
  "mcpServers": {
    "cityengine": {
      "command": "python",
      "args": ["-m", "cityengine_mcp.server"],
      "env": { "CITYENGINE_API_KEY": "cvk_xxx" }
    }
  }
}
```

Ready-made snippets for **kimi code**, **Codex CLI** and **Claude Desktop** live in [`config-examples/`](config-examples/).

## OpenAI-compatible function calling (`cityengine-tools/`)

```bash
pip install ./cityengine-tools
```

```python
from cityengine_tools import CityEngineTools
tools = CityEngineTools(api_key="cvk_xxx")
# pass tools.schemas() as the `tools` parameter of any OpenAI-compatible chat call;
# feed tool_calls back into tools.dispatch(...) to execute them.
```

JSON schemas for all six tools are in `cityengine-tools/schemas/`.

## Links

- Website & demos: https://www.cityengine.cn · plugin page: https://www.cityengine.cn/plugins.html
- API: https://api.cityengine.cn (OpenAPI contract `openapi-v1.yaml`)

## License

MIT (see [LICENSE](LICENSE)). The packages are client SDKs; generated models and CGA output are governed by your cityengine.cn plan terms.
