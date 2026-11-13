# Directory check (Glama)

You do not need anything in this folder to use the module. Install `calderkit_mcp` in your Odoo and connect your MCP client to `/mcp`, as the main [README](../README.md) explains.

MCP directories such as [Glama](https://glama.ai/mcp/servers) check a server by starting it in a container and asking for its tools. This module lives inside Odoo, so the container needs an Odoo. These two files build and start a throwaway one:

- `build.sh` installs PostgreSQL and Odoo Community (same series as this branch, only `base`, `web` and `rpc`) on Debian 13, creates a database with this module and demo data, and an API key for its administrator. It runs as root from the repository root: `sh glama/build.sh`.
- `serve.py` starts that database and Odoo on 127.0.0.1, then passes MCP messages between standard input/output and the `/mcp` endpoint. Standard library only.

Glama settings: build steps `["sh glama/build.sh"]`, command `["python3", "glama/serve.py"]`.

Everything stays inside the container. The demo database holds only Odoo's sample data, and its API key works nowhere else.
