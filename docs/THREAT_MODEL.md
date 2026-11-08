# Who can do what on a pondNT box

pondNT hands an AI agent the keys to a D365 development box: it runs
commands elevated, as the person who installed it, and can build, sync,
restart and write to the database. That is the point, and it is also the
risk. This page says what pondNT enforces, what it deliberately does not,
and where the line runs between what it controls and what it cannot.

The short version: **pondNT is for dev boxes that are allowed to get dirty:
a box you can throw away and rebuild, with no live data on it and no data you
cannot afford to lose. Never on a box connected to production. And treat
whatever the agent reads as able to steer it.**

## The callers

Every MCP server listens on 127.0.0.1 only and refuses a call without a
valid access key (`Authorization: Bearer bsk_...`). A key belongs to one
server and one person, and lives 365 days; running setup with fewer than 30
left renews it.

Two additional loopback surfaces sit outside the MCP bearer-key check:

- **Canvas's web page** (port 18510) opens with a token in its URL, the
  link the Canvas tools hand out; whoever has the link sees and edits that
  canvas.
- **The d365web browser's debugging port** (`debug_port` in `d365web.json`,
  9333 in the examples; form-mode d365whsapp normally uses 9334) has no authentication at all. While that Edge runs,
  any local program can drive the signed-in D365 session through it, as
  that user. Close the browser (`web_close`, or `idle_minutes`) when it is
  not needed.

- **Where keys are.** The plain keys are in `%USERPROFILE%\.mcp.json`
  (and, for services behind the gateway, in its `<service>.headers.json`),
  in your own profile. Each server's `keys.json` stores only a fingerprint.
  Anyone who can read your profile can call the servers as you; so can any
  program running as you.
- **Roles.** A server knows two roles: `reader` (status, results, xref,
  reading files and jobs) and `writer` (everything that runs or changes
  something). setup gives Copilot writer keys, because a development agent
  needs them. A reader key for a narrower client can be added by hand, for
  any server but the core:

      & "$p\boring-service-keys.cmd" add --file "$env:LOCALAPPDATA\boring-pondnt\services\d365\keys.json" --subject robin@devbox --role reader --days 90 --label "read-only client"

  It is only narrower if that client gets no core key at all, of either
  role: the core's `exec` can do anything a reader key forbids, and the
  core's `file_read`, a reader tool, reads any file you can, `.mcp.json` and
  its writer keys included. Every core key is effectively a full key. Give core keys only to trusted
  callers; there is no credential-free reader view of the core.

## What a writer can do

| Server | Runs as | A writer can |
|---|---|---|
| core | you, elevated | run any command, read and write any file (absolute local paths; no roots, no sandbox; network shares, device paths, alternate data streams and device names refused) |
| d365 | you, elevated | build, sync (module or full), best practice, `d365_run` (any d365fo.tools command, including stopping and restarting the environment), `d365_sql` (any statement on any local database, with your Windows login) |
| d365web, d365whsapp | you, elevated | act in D365 as the signed-in user or warehouse worker |
| d365data | you, elevated | read and change OData entities and run DMF packages with the permissions of the configured Entra app's mapped D365 user |
| Sourcecache, Knowledge, Canvas, gateway | SYSTEM | search the index; write notes and drawings; reach the services behind the gateway with their keys |

These are your rights, used by an agent. On a dev box they are what the
work needs. On a box whose D365 talks to production data or other people's
environment, they are a production change without review.

## What pondNT enforces

- Loopback only; a key for every MCP call (the two doors above excepted);
  role checks per call, with the core credential-disclosure limit above.
- No argument text in the call log ([FOUNDATION_SPEC](FOUNDATION_SPEC.md#call-log)).
- The release folder: only administrators and SYSTEM may change it (and
  its owner, who can always change permissions; normally that is the
  administrator who unpacked it), because its Python runs as SYSTEM and
  elevated. setup sets this and checks every file below it
  ([INSTALL](INSTALL.md#1-download-check-and-unpack)).
- Bounded calls: timeouts, row limits, result sizes; a lost reply is
  reported as unknown, never resent blindly.

General services remain SYSTEM boot tasks. The release-folder lockdown
protects their executable payload; it does not make their profile-based
configuration immutable. Core and the D365 backends are interactive logon
tasks. A gateway that starts at boot cannot make them available before the
developer logs on.

## What it deliberately does not

- **No confirmation per call.** Visual Studio asks before a tool call
  unless you allowed it permanently; a second prompt from pondNT would be
  clicked through just the same.
- **No read-only SQL switch, no environment allowlist.** A setting that
  limits `d365_sql` does not limit an agent that can run `sqlcmd` through
  the core, elevated. A rule that cannot hold is worse than none, because
  people rely on it. Where a narrower client matters, give it reader keys
  and no core.
- **No check of which environment it is on.** pondNT does not know whether
  the AOS behind the box serves a throwaway database or a shared one. You
  do.
- **No guard on destructive D365 operations.** `d365_run` can stop the
  environment, and a full sync keeps it busy for a long time. `dry_run`
  exists for build, sync and best practice; the rest runs when called.

## What it cannot control

- **The model.** Whatever the agent reads (source, a work item, a web page,
  the database) can contain instructions, and the agent may follow them with
  your rights. pondNT cannot tell a good instruction from a planted one.
- **Your profile.** The SYSTEM services read their configuration and state
  from your `%LOCALAPPDATA%\boring-pondnt`. A program running as you, not
  elevated, can change those files; that is a path from your unelevated
  session to code that runs as SYSTEM. Windows does not treat that step
  (UAC) as a security boundary, and neither do we; on a box where it
  matters, nobody untrusted runs as you.
- **Defender.** The optional exclusion stops scanning what pondNT's Python
  opens ([INSTALL, step 2](INSTALL.md#2-defender-optional----dangerous-read-first)).

## Designs not implemented

These are possible future boundaries, not settings or guarantees in this
release:

- a core reader view that cannot read credentials;
- configurable core file roots or a constrained command identity;
- dedicated least-privilege Windows service or database identities;
- environment allowlists, target-identity checks and destructive-operation guards;
- starting the core and D365 backends without the developer's logon.

The standard installer keeps SYSTEM for general services. A dedicated D365
browser test user is optional and signed in per profile
([browser setup](D365WEB.md#setup)); it does not constrain core command access.
SQL advice or a restricted tool alone cannot constrain a caller that also
has arbitrary elevated execution.

## Before you install

Use a box you can rebuild. Read what setup will change (`-WhatIf`). Keep
`.mcp.json` private. If the box can reach anything that matters beyond
itself, decide that first, with the people it belongs to.
