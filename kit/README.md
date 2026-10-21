# The kit

The working method served by boring-pondnt-core through bootstrap. A release
ships it as `<release>/kit`; setup.ps1 copies it once to
`%LOCALAPPDATA%\boring-pondnt\kit`, and the core serves that copy. These are
shared guidance documents, not permissions or proof that a particular machine
behaves as described.

Write for models working inside Copilot: short, literal instructions, concrete
syntax and discoverable tool names. Keep the action beside its condition. Scope
established facts to the evidence; do not weaken them into anecdotes merely to
sound cautious. Preserve examples that teach the craft or the voice.

## Reading map

Every fresh context loads [bootstrap](bootstrap.md), [agreement](agreement.md),
[housekeeping](housekeeping.md) and the short [persona](persona.md), followed
by the project's own instructions and current handoff.

Read the relevant task guides:
- [Visual Studio](visualstudio.md): VS/Copilot ownership, source mapping,
  edits, evidence and recovery.
- [Engineering practice](practice.md): implementation, investigation,
  testing, reports and proportionate use of context.
- [D365](d365.md): extension mechanisms, data semantics and acceptance.
- [Warehouse/mobile](warehouse.md): request state, transactions and device tests.
- [Tools](tools.md): core/D365 job ownership, SQL and optional services.

The [repository snippet](copilot-instructions.md) can be merged into an existing
.github/copilot-instructions.md through the owner's permitted IDE workflow.
Preserve organization/repository instructions; do not replace them wholesale.

Fitzwilliam's voice is optional: “Plain, please” disables it for the session.
Authority and engineering standards do not depend on the persona.

## Adaptation and updates

Keep project/customer context in the project's approved records, not this
redistributable kit. The served kit is the owner's copy outside the release
folders: a later release never overwrites it. Setup reports which files differ
from that release's kit, and bootstrap names the release's kit as
`release_kit`; review and merge those changes deliberately with the owner, do
not quietly rewrite the agreement. A changed kit is not loaded into an existing
conversation until it is read again.

Validate a changed kit with a fresh conversation: required reads, ordinary
authorized work, IDE failure without an external workaround, lost context,
stale evidence and report-only behavior. Reading and text checks alone do not
establish Copilot, designer, build or device behavior.

## Origin

The agreement and practice derive from an earlier working kit for Copilot,
shaped by Wald's collaboration with the Boring Crocodile.
Fitzwilliam's early D365 work contributed lessons about IDE paths, read-back,
metadata, builds, device testing and continuity; a review separated durable
practices from observations and kept the concrete craft examples.

Living project knowledge is deliberately not bundled as guidance. Methods and mechanisms live with their owners;
project state stays with its project.

The kit is CC0 as described in the repository's COPYING.md.
