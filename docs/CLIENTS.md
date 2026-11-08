# Other clients on the same box

pondNT's servers speak MCP over HTTP on 127.0.0.1, each with its own access
key. setup.ps1 writes them for Visual Studio, and that is the client we test.
Another client needs Streamable HTTP MCP and support for the configured
bearer header; an arbitrary HTTP client is not enough. Visual Studio 2022
17.14 in Copilot Agent mode is the recorded client
([Visual Studio notes](VISUAL_STUDIO_COPILOT.md)). The VS Code and Claude Code
recipes below follow vendor documentation and **have not been run against
pondNT**. Claude Desktop has no verified direct recipe here. Check the actual
client/runtime version with one read-only call (`bootstrap` on the core).

## What to copy

After setup, `%USERPROFILE%\.mcp.json` holds one entry per server Visual
Studio reaches directly:

    "boring-pondnt": {
      "type": "http",
      "url": "http://127.0.0.1:18513/mcp",
      "headers": { "Authorization": "Bearer bsk_..." }
    }

Copy the URL and the key into the other client. With the recommended layout
there are three entries: the core, Knowledge and the gateway
(boring-gateway); the gateway reaches the other services, so a client
needs nothing more.

Two things follow from copying:

- **The key is a password for that server.** The core's key runs commands
  elevated, as you. Keep it out of anything shared or committed; where a
  client can prompt for a secret, let it.
- **setup renews its managed keys, not your copies.** Keys last 365 days;
  running setup with fewer than 30 left rotates them through its managed
  client entries or gateway headers. It does not update another client's
  configuration. After old-key retirement, update those copies from the
  current managed entry; never put a populated key in a shared project file.

## VS Code's MCP client (untested)

VS Code reads its own `mcp.json`: **MCP: Open User Configuration** in the
command palette opens it ([VS Code's reference](https://code.visualstudio.com/docs/agents/reference/mcp-configuration)).
An `inputs` entry makes VS Code ask for the key once and store it, instead
of keeping it in the file:

    {
      "inputs": [
        { "type": "promptString", "id": "pondnt-core", "description": "pondNT core key", "password": true }
      ],
      "servers": {
        "boring-pondnt": {
          "type": "http",
          "url": "http://127.0.0.1:18513/mcp",
          "headers": { "Authorization": "Bearer ${input:pondnt-core}" }
        }
      }
    }

This is the VS Code server format. Microsoft's reference distinguishes it
from portable configuration and Copilot harness forwarding: servers needing
`${input:...}` prompts are not forwarded to those harness sessions. Do not
treat a connection in one VS Code runtime as acceptance in another.

## Claude Code (untested)

The [Claude Code MCP reference](https://code.claude.com/docs/en/mcp#option-1-add-a-remote-http-server)
documents HTTP transport and an Authorization header.
Options come before the server's name; `--scope user` makes it available in
every project and stores it in your `~/.claude.json`:

    claude mcp add --scope user --transport http boring-pondnt http://127.0.0.1:18513/mcp --header "Authorization: Bearer bsk_..."

`claude mcp list` shows whether it connects.

## Claude Desktop (no verified direct recipe)

Anthropic's [Desktop guide](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop)
describes local server extensions and their configuration. That does not
establish a direct connection to pondNT's existing loopback HTTP endpoint
with its bearer key. No native HTTP configuration schema or third-party
stdio bridge is asserted here. Desktop and Claude Code are separate clients;
the Code command above is not a Desktop recipe.

## Not on this box

These servers listen on 127.0.0.1 only. A client on another machine does
not reach them, by design; [reaching the box from a Pond](INSTALL.md#optional-reaching-the-box-from-a-pond)
is the supported way in from outside.
