# MCP Server for Odoo

An Odoo module that turns your Odoo database into a **Model Context Protocol (MCP) server**. Claude, ChatGPT, Cursor, VS Code and any other MCP client can then search, read, count and total your Odoo records, and answer questions about them in plain language. Every request runs with the signed-in user's own access rights, only on the models an administrator allows, and is logged.

- **Odoo 18, 19 and 20**, Community and Enterprise, on Odoo.sh or your own server
- **Free and open source** (LGPL-3), read-only
- Streamable HTTP endpoint at `/mcp` inside Odoo, with no extra server, no Docker and no outside service

This branch is for **Odoo 20**. Other versions: [`20.0`](https://github.com/Calderkit/odoo-mcp-server/tree/20.0), [`19.0`](https://github.com/Calderkit/odoo-mcp-server/tree/19.0), [`18.0`](https://github.com/Calderkit/odoo-mcp-server/tree/18.0).

Product page: [calderkit.com/odoo-mcp-server](https://calderkit.com/odoo-mcp-server) · Odoo Apps store: [MCP Server](https://apps.odoo.com/apps/modules/20.0/calderkit_mcp)

## Example

On the Odoo demo database, you ask: *"Which confirmed sales orders are the largest?"* The assistant calls `odoo_search` on `sale.order`, filtered on confirmed orders and sorted by total, and answers: *"S00020 for Joel Willis (3,389.63), then S00004 (2,240.00) and S00007 (1,706.00), both for Gemini Furniture."*

## Tools

| Tool | What it does |
| --- | --- |
| `odoo_context` | Who is connected, the company, the language, the time zone and today's date, so "last quarter" means the right dates |
| `odoo_list_models` | The models the assistant may use, with a short hint for each |
| `odoo_describe_model` | The fields of a model: type, label, relation, selection values |
| `odoo_search` | Search records with any Odoo domain, sorted and paged, with the total number of matches |
| `odoo_read` | Read records by ID |
| `odoo_count` | Count the records that match a domain |
| `odoo_aggregate` | Totals by group, for example revenue per customer per month, without reading every record |

Out of the box the server offers common business models (contacts, products, sales, purchases, invoices, CRM, projects, inventory) when those apps are installed. An administrator adds or removes models in **MCP Server → Configuration → Models**.

## Security

- **The user's own rights.** The assistant authenticates as a person and sees exactly what that person sees in Odoo: access rights, record rules, companies and field groups all apply.
- **Only the models you allow.** Users, settings and technical models cannot be exposed. Administrators can hide more fields per model.
- **No secrets.** Password, token, key and signature fields and binary files are never returned, and they cannot be used in filters either.
- **A key only for AI** (Odoo 20). The module adds an "MCP (AI assistants only)" scope to API keys; such a key cannot be used for any other API.
- **Everything logged.** Each call is recorded with the user, tool, model, filters, number of records, duration and any error, in **MCP Server → Calls**.
- **No delete tool, no write tool.** This free module is read-only.
- **Nothing goes through us.** The module answers only the MCP client you connect and makes no outside connection of its own.

## Install

From the Odoo Apps store: [MCP Server](https://apps.odoo.com/apps/modules/20.0/calderkit_mcp) (free).

From this repository:

```bash
git clone --branch 20.0 --depth 1 https://github.com/Calderkit/odoo-mcp-server.git
cp -r odoo-mcp-server/calderkit_mcp /path/to/your/addons/
```

Restart Odoo, open **Apps**, click **Update Apps List**, search for "MCP Server" and install it. Administrators get access at once; give other users the **MCP Server / User** role.

## Connect an AI assistant

1. In Odoo, open your user menu, then **Preferences** (or **My Preferences**), tab **Security**, and create an API key. On Odoo 20, choose the scope **MCP (AI assistants only)**.
2. Add the server to your client. The module's **MCP Server → Connect** page shows these snippets with your own address.

Claude Code:

```bash
claude mcp add --transport http odoo https://your-odoo.example.com/mcp \
  --header "Authorization: Bearer YOUR_API_KEY"
```

Cursor, Claude Desktop, VS Code and other clients that accept a JSON configuration:

```json
{
  "mcpServers": {
    "odoo": {
      "type": "http",
      "url": "https://your-odoo.example.com/mcp",
      "headers": { "Authorization": "Bearer YOUR_API_KEY" }
    }
  }
}
```

3. Ask, for example: *"Which customers bought the most last quarter?"*

## Protocol support

- MCP 2026-07-28: stateless requests, `server/discover`, per-request protocol version, `Mcp-Method` and `Mcp-Name` header checks.
- MCP 2025-11-25, 2025-06-18 and 2025-03-26 through the `initialize` handshake.
- Streamable HTTP transport, JSON responses. Bearer authentication with Odoo API keys.
- Tested with the official MCP Python SDK in both modes, on Odoo 18, 19 and 20.

## Requirements and limits

- Odoo 18.0, 19.0 or 20.0. Use the branch that matches your Odoo version.
- Not available on Odoo Online, where Odoo does not allow modules with Python code. Odoo.sh and self-hosted servers work.
- The address `/mcp` must reach the right database: the server hosts one database, or a database filter (`dbfilter`) maps the hostname to it. Odoo 19 and 20 also accept an `X-Odoo-Database` header.
- Web connectors that only sign in with OAuth (Claude.ai, ChatGPT) cannot use API keys. They need MCP Server Pro.

## MCP Server Pro

[MCP Server Pro](https://apps.odoo.com/apps/modules/20.0/calderkit_mcp_pro) ($89 per Odoo version, on the Odoo Apps store) adds:

- OAuth 2.1 sign-in for web connectors such as Claude.ai and ChatGPT, following the MCP authorization specification (protected resource metadata, client ID metadata documents, dynamic client registration, PKCE, refresh token rotation)
- Create and update tools, per model: read only, approve first, or allowed
- Approval of changes inside Odoo before anything is written
- Daily limits per user, and a list of connected applications that can be disconnected

## Questions

**Odoo 20 Enterprise has its own MCP. Why this module?**
It works on Community and on Odoo 18, 19 and 20, does not use Odoo AI credits, lets you choose the exposed models, and logs every call.

**Where does my data go?**
Only to the AI application a user connects, in answer to its requests, and from there to the AI provider behind it.

**Can the assistant change or delete records?**
Not with this module. It has no write or delete tools.

## Support

Questions and bug reports: [odoo@calderkit.com](mailto:odoo@calderkit.com). We answer in writing within two business days.

## License

LGPL-3.0. See [LICENSE](LICENSE). Made by [Calderkit](https://calderkit.com).
