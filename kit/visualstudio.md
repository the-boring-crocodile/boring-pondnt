# Working inside Visual Studio

Read before solution work, metadata changes, builds or IDE troubleshooting.

## The owner boundary

**Everything Visual Studio manages is handled through VS/GitHub Copilot
tooling.** This is an explicit working requirement, not a cache hypothesis.

It covers creation, editing, renaming, deletion, project membership,
designer-backed metadata, source-control actions, build/debug operations and
cleanup of generated artifacts. A file need not appear in the open-files list
to be VS-managed. A package link and its repository target may be the same file.

Do not substitute pondNT file writes, PowerShell, hand-written model XML,
an alternate path or manual XppMetadata deletion. An IDE tool failure does
not change authority. If the required IDE capability is unavailable, prepare
the smallest exact action for the owner to perform in VS, then verify its
result. Do not claim to have operated a designer you cannot control.

pondNT can support work outside this ownership boundary. Independent,
read-only inspection is evidence, never a route to manage VS state. If the
owner's task restricts even that inspection to VS tools, follow the narrower
scope. External build/sync tools are not substitutes for this VS workflow.

## Establish what the IDE owns

Find the actual workspace, source root, model/package mapping and source-control
state before changing anything. The workspace can be nested inside the
repository; a package directory can link to repository metadata. Discover the
configured service drive rather than assuming a drive letter.

Inspect pending changes using the available VS/source-control tooling. A clean
Git diff is not proof that an untracked or ignored model is unchanged.
Preserve an approved baseline outside model folders when needed, and compare
before/after. Do not reset, add or remap files merely to make evidence easier.

The open-files list is a clue to the current discussion, not complete project
state or evidence that its buffers are saved.

## Edit and verify

Inspect this session's tool list. Known VS/Copilot names include `get_file`,
`create_file`, `replace_string_in_file` and `multi_replace_string_in_file`;
some sessions expose `edit_file` or `apply_patch`. Use the IDE-owned tool,
not an external namesake. Read the path from the `get_file` header and use
that path for the edit; do not reconstruct it from memory.

Read current content. For replacement tools, use unique full-line
`oldString`/`newString` spans with enough context. Group compatible edits to
one file in `multi_replace_string_in_file` when available. Read back changed
and neighbouring lines: a successful call can still remove an adjacent line.
If these tool names are absent, discover their exposed equivalent; never invent
a capability or fall back to pondNT writes.

For metadata, XML parsing establishes syntax only. Designer persistence,
project membership, compilation and runtime behavior are separate checks.
Use the designer's saved representation and documented extension mechanism.
New elements are created through VS; obtain an owner-operated representative
element if necessary. Do not invent serialization or repair it from memory.

For an unresponsive control, check the loaded build and reopen the form before
changing serialization. Stale code caused this symptom in one recorded case;
it is a candidate explanation, not a diagnosis of every dead button.

Check encoding/bytes through an authorized read route before replacing text
that only looks garbled. Preserve valid encoding, BOM and line endings.

## Builds and evidence

Arrange one coherent verification for the change; do not build after each
keystroke or compete with a build already running. Model, package and solution
are distinct scopes. Identify which one the build actually covered.

Use the VS/Copilot build and diagnostic route. If the owner builds, record the
result as reported, with scope/time and relevant diagnostics. If a read-only
pondNT diagnostic inspection is appropriate, d365_results may find compatible
package logs from a VS build. Absence of such a log proves no build verdict.

A log's path and timestamp must match the intended build. Latest available is
not necessarily current, complete or tied to the edited source. Runtime
execution adds evidence; it does not retrospectively prove all build checks.

Schema changes require the appropriate successful build and synchronization
before dependent tests. Use the approved VS workflow; a missing capability
needs an owner handoff. Distinguish build success, sync success and functional
acceptance. Record remaining gaps.

## IDE recovery

Classify the failure before acting: wrong workspace/path, tool absence,
unsaved state, disconnected server or an unexpected editor error.
Read the path exposed by the IDE tool. Verify an alternative path remains
inside the authorized workspace; never use aliases to defeat a restriction.

After an unexpected text-view/session failure, save the handoff and ask the
owner to save work and restart VS, then retry the IDE tool. Do not restart
over unsaved work or a running build. For missing tools, inspect the session's
actual tool list and GitHub Copilot Output diagnostics before inferring absence
from a picker alone. A connection icon is not a successful tool call.

Keep settings, trust, exclusions and permissions unchanged unless explicitly
authorized. Stop repeating an identical failing route without new evidence.

MCP discovery locations and lifecycle are version-specific. Check the installed
VS version and [Microsoft's MCP guide](https://learn.microsoft.com/en-us/visualstudio/ide/mcp-servers).
The recurring path/edit/restart incidents are observations from particular
sessions; their proposed cache/workspace causes were not universal findings.
