# Housekeeping

Leave every change intended and everything else accounted for.

## Authority

Routine edits and checks within the authorized task proceed without asking
the same permission twice. Ask before expanding scope, deleting unique or
unrelated work, changing access/settings, or interrupting services, processes
or another person's work. Existing authorization counts within its scope.
Technical access is not ownership.

Everything Visual Studio manages stays with VS/GitHub Copilot tooling:
[visualstudio.md](visualstudio.md) owns this boundary, including cleanup.
Preserve unrelated pending changes. Leave your changes pending by default;
commit/check-in only when authorized and through the project's permitted IDE
workflow. Never revert someone else's work to obtain a clean build.

## Files, jobs and test data

Keep scratch outside source and model folders, normally in a task folder under
`%LOCALAPPDATA%\boring-pondnt\scratch`. No backup copies in model folders.
Preserve useful failure evidence and a baseline where version control cannot.
Clean only identified task-owned artifacts, using their owner's supported tools.
Do not infer ownership from creation time or a name prefix alone.

Coordinate with the owner: **one database sync at a time; one build per
package at a time**. Never leave work running unannounced or interrupt an IDE
build. Before substantial work, check relevant capacity and state; estimate
duration only from evidence.

Restarting AOS ends browser sessions and debugging; say so before requesting
it. If VS owns IIS Express, use the IDE route. Batch jobs that post, send or
integrate are business processes: obtain explicit task authority before
starting them, even for a test.

Test setup and cleanup belong to the agreed test scope. Record the exact
environment, company, records, reset behavior and retention choice. Use
application mechanisms for business processes. Direct SQL needs the checks
in [tools.md](tools.md); a prefix is not sufficient proof that deletion is safe.
Keep needed fixtures until their acceptance/evidence purpose is complete.

## Privacy and continuity

Business data stays on the box. Return the minimum answer, not copied tables
or exports to external files/services.

Keep credentials out of output and notes. Redaction is a limited backstop,
not permission to run commands that print secrets. If a secret appears,
do not repeat it; tell the owner so they can assess rotation.

Project state belongs in approved private project records. Shared notebooks
receive only appropriate sanitized lessons, with audience and access checked.
Future sessions can read saved context; agree what should persist.

## Closing inventory

At task completion or a meaningful handoff, summarize what changed, what
remains running, what was retained intentionally and what was cleaned up.
Combine this with the result and remaining verification. A read-only task
needs at most “changed: nothing”; ordinary conversation needs no inventory.

For artifacts and state you introduce: **what is not in the inventory should
not be on the box.** This does not authorize removing pre-existing work.
