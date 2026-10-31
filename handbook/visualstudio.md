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

Record the VS, Copilot and pondNT versions and inspect this session's native
tool schemas. The path/edit examples below were observed in VS 2022; they are
starting points, not established VS 2026 behavior. An installed extension's
manifest version does not independently prove which version the IDE loaded.

Find the actual workspace, branch, source root, model/package mapping and
source-control state before changing anything. The workspace can be nested
inside the repository; a package directory can link to repository metadata. Discover the
configured service drive rather than assuming a drive letter.

Inspect pending changes using the available VS/source-control tooling. A clean
Git diff is not proof that an untracked or ignored model is unchanged.
Preserve an approved baseline outside model folders when needed, and compare
before/after. Do not reset, add or remap files merely to make evidence easier.

An empty project list or wildcard search does not prove files are absent.
Check the current branch and a literal path through an authorized read route.
The open-files list does not establish project completeness or saved buffers.

## Edit and verify

Inspect this session's tool list. Known VS/Copilot names include `get_file`,
`create_file`, `replace_string_in_file` and `multi_replace_string_in_file`;
some sessions expose `edit_file` or `apply_patch`. Use the IDE-owned tool,
not an external namesake.

Readers and editors can resolve the same linked model differently. Establish
the package-to-repository mapping; do not guess paths or treat aliases as copies.
This combination worked in a VS 2022 linked-model workspace:

| Operation | Native tool | Path form |
| --- | --- | --- |
| Read before/after | `get_file` | `<drive>:\AosService\PackagesLocalDirectory\<package>\<model>\AxClass\<name>.xml` |
| Edit existing source | `apply_patch` or `edit_file` | `..\<source-repo>\Metadata\<package>\<model>\AxClass\<name>.xml`, relative to the solution |

Package-path patches failed with FileNotFound while reads succeeded; the
repository-relative reader also failed while editors succeeded. If this occurs,
retry the same native editor once with the verified solution-relative path.
If needed, try another available native editor separately. Read back through
the proven reader path to verify the active model changed. Record the working
path per tool in project context. Restarting VS did not repair that path failure.
These observations establish existing-file edits, not new-element creation.

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

Use the VS/Copilot build and diagnostic route. Record owner-run results as
reported, with scope/time and diagnostics. Match logs to the intended candidate
and completed build; runtime success does not prove the build checks passed.
For authorized read-only `d365_results` inspection and its limits, see
[tools.md](tools.md#d365-operations-and-evidence).

Schema changes need a successful build and sync before dependent tests.
Keep build, sync and functional verdicts separate; name missing capabilities
and remaining checks in the owner handoff.

## IDE recovery

Classify the failure before acting: wrong workspace/path, tool absence,
unsaved state, disconnected server or an unexpected editor error.
Read the path exposed by the IDE tool. Verify an alternative path remains
inside the authorized workspace; never use aliases to defeat a restriction.

For an unexpected text-view/session failure, inspect Copilot Output diagnostics
and try one harmless native read. If a restart is warranted, save the handoff
and coordinate it with the owner; never interrupt unsaved work or a build.
A restart is not a generic path fix. For missing tools, inspect the actual tool
list rather than a picker alone. A connection icon is not a successful call.

Keep settings, trust, exclusions and permissions unchanged unless explicitly
authorized. Stop repeating an identical failing route without new evidence.

MCP discovery locations and lifecycle are version-specific. Check the installed
VS version and [Microsoft's MCP guide](https://learn.microsoft.com/en-us/visualstudio/ide/mcp-servers).
The recurring path/edit/restart incidents are observations from particular
sessions; their proposed cache/workspace causes were not universal findings.
