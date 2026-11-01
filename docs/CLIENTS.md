# Other clients on the same box

pondNT's servers speak MCP over HTTP on 127.0.0.1, each with its own access
key. setup.ps1 writes them for Visual Studio, and that is the client we test.
Any client on the same box that can send an HTTP request with an
`Authorization` header can use them too. The recipes below follow each
client's own documentation; **we have not run them**, so treat them as a
starting point and check the result with one read-only call (`bootstrap` on
the core).

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
- **setup renews keys, not copies.** A key lives 365 days; running setup
  with fewer than 30 left issues a new one and updates `.mcp.json` only.
  Other clients keep the old key and are refused until you copy it again.

## VS Code (GitHub Copilot, agent mode)

VS Code reads its own `mcp.json`: **MCP: Open User Configuration** in the
command palette opens it ([VS Code's reference](https://code.visualstudio.com/docs/copilot/reference/mcp-configuration)).
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

## Claude Code

Options come before the server's name; `--scope user` makes it available in
every project and stores it in your `~/.claude.json`:

    claude mcp add --scope user --transport http boring-pondnt http://127.0.0.1:18513/mcp --header "Authorization: Bearer bsk_..."

`claude mcp list` shows whether it connects.

## Claude Desktop

Claude Desktop's configuration file (`%APPDATA%\Claude\claude_desktop_config.json`)
starts local programs over stdio; it has no field for an HTTP address, and
its custom connectors sign in with OAuth, which these servers do not offer.
The way that remains is a stdio-to-HTTP bridge such as the third-party
`mcp-remote` package (it needs Node.js), started by Claude Desktop with the
key in its environment. That adds a program we neither ship nor test
between the client and an elevated server; weigh that before using it.

## Not on this box

These servers listen on 127.0.0.1 only. A client on another machine does
not reach them, by design; [reaching the box from a Pond](INSTALL.md#optional-reaching-the-box-from-a-pond)
is the supported way in from outside.
