# Copilot in Visual Studio: tips and tricks

What we learned connecting Copilot Chat in Visual Studio to an MCP server
that signs clients in itself (OAuth), and the Visual Studio habits that cost
time. Tested on a D365 development box (Visual Studio Professional 2022,
17.14) against a test server (boring-knowledge with its own issuer).
Unless a note says otherwise, the observations below belong to that tested
build. They do not establish current Visual Studio 2026 behavior. The
[current Microsoft guide](https://learn.microsoft.com/en-us/visualstudio/ide/mcp-servers)
documents remote OAuth and Manage Authentication; it does not establish a
universal DCR requirement. See the family's
[client guide](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/clients.md)
for current documented setup paths and the acceptance checks.

## Recommended pondNT layout

Use [setup with `-Gateway all`](INSTALL.md#the-short-way-setupps1).
Copilot connects directly to core and Knowledge and discovers the other
selected services through Ferry. Expect `boring-pondnt`, `boring-knowledge`
and `boring-gateway` in the client configuration, not a direct entry for
every backend. Ferry's `service_list` and `service_describe` reveal those
backends and their tools. This keeps workspace and notes immediately
available while specialist tools are discovered when needed.

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

- **The tested Visual Studio 2022 17.14 build needed dynamic registration.**
  It started sign-in only when the server metadata advertised a
  `registration_endpoint`. In that test it neither used a client metadata
  document nor asked for a client ID. With registration off it stopped after
  discovery; the Output pane said "Token is not available".
  Enabling `"issuer": {"dynamic_registration": true, ...}` on the test issuer
  allowed sign-in. The approval secret still guarded token issuance.
  Recheck the installed client build before changing a site's registration
  policy; this observation is not a requirement for every Visual Studio version.
- It registered as "Visual Studio Copilot" with a loopback callback on a
  random port (`http://127.0.0.1:<port>/`); the approval page warns that any
  local program can claim that name. Expected.
- After sign-in the browser showed an empty page and Visual Studio a green
  "Authenticated". Confirm a tool call and identity as well; that indicator
  alone does not establish refresh acceptance.

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
  ([Microsoft: MCP servers](https://learn.microsoft.com/visualstudio/ide/mcp-servers)).
  Current setup can keep an unchanged `.mcp.json` without writing it or making
  a backup. After a real save in the tested build, the tools picker sometimes
  listed our servers but no **GitHub Copilot** group, and agent mode could not
  use Visual Studio's own tools. Restarting Visual Studio, or signing in to
  GitHub again, brought them back. The servers kept running throughout.
- **Enter the secret once, do not go back.** An approval request is used up
  on success; resubmitting or reloading the page answers 400.
- **What it decided:** View → Output → "Show output from" → Model Context
  Protocol (or GitHub Copilot). The server's log shows the other half:
  `/register`, `/authorize`, `/approve`, `/token`, then `/mcp`.
