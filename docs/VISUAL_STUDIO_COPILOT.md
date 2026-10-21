# Copilot in Visual Studio: tips and tricks

What we learned connecting Copilot Chat in Visual Studio to an MCP server
that signs clients in itself (OAuth), and the Visual Studio habits that cost
time. Tested on a D365 development box (Visual Studio Professional 2022,
17.14) against a test server (boring-knowledge with its own issuer).

## Adding a server

- **UI:** Copilot Chat → Agent mode → the tools icon (wrench) → green **+**
  ("Add custom MCP server"). Server ID, type HTTP, URL, destination Global.
  Older 17.14 builds have no **+**; update first (Help → Check for Updates).
- **File:** the same entry by hand in `%USERPROFILE%\.mcp.json` (all
  solutions), under the top-level `"servers"` object:

      "test-server": { "type": "http", "url": "https://knowledge-url.example.test/mcp" }

## Signing in (OAuth)

pondNT itself does not use this: its servers listen on 127.0.0.1 and setup puts
an access key into each `.mcp.json` entry. What follows matters when
Visual Studio connects to a server that signs people in itself.

- **Visual Studio needs dynamic client registration.** It reads the server's
  metadata, and only starts a sign-in when the metadata names a
  `registration_endpoint`. It does not use client ID metadata documents
  (Claude, ChatGPT do) and does not ask for a client ID. With registration
  off it silently stops after discovery; the Output pane says
  "Token is not available".
  So an issuer that Visual Studio uses needs
  `"issuer": {"dynamic_registration": true, ...}`. The approval secret still
  guards it: anyone can register, nobody gets a token without the secret.
- It registers as "Visual Studio Copilot" with a loopback callback on a
  random port (`http://127.0.0.1:<port>/`); the approval page warns that any
  local program can claim that name. Expected.
- After a successful sign-in the browser shows an empty page and Visual
  Studio a green "Authenticated". That is success.

## Visual Studio habits

- **A failed or half-done sign-in sticks to the server name.** Retrying,
  even after restarting Visual Studio, did nothing. **Renaming the server**
  in `.mcp.json` made Visual Studio start fresh (new registration, new
  sign-in). The server keeps the abandoned registration; remove it with
  `boring-service-oauth --db <db> client-remove <id>` or let it expire
  (unused clients go after a day).
- **New tools appear only in a new chat thread.** After connecting, the
  server already had the tool list (the server log shows it), but the open
  thread did not; the **+** for a new thread did. Also check the server's
  checkbox in the tools list.
- **Saving `.mcp.json` can take Copilot's own tools away.** Visual Studio
  restarts the Copilot agent whenever `.mcp.json` is saved
  ([Microsoft: MCP servers](https://learn.microsoft.com/visualstudio/ide/mcp-servers)),
  and setup saves it on every run. Afterwards the tools picker sometimes lists
  our servers but no **GitHub Copilot** group, and agent mode cannot use
  Visual Studio's own tools. Restarting Visual Studio, or signing in to GitHub
  again, brings them back. A Visual Studio matter: the servers keep running
  throughout.
- **Enter the secret once, do not go back.** An approval request is used up
  on success; resubmitting or reloading the page answers 400.
- **What it decided:** View → Output → "Show output from" → Model Context
  Protocol (or GitHub Copilot). The server's log shows the other half:
  `/register`, `/authorize`, `/approve`, `/token`, then `/mcp`.
