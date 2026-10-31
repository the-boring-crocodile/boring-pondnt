# Warehouse and mobile work

Read with [d365.md](d365.md) when the task involves SCM warehouse processes.

## Trace one real business path

Establish menu item/mode, company, warehouse/user context, document, dimensions,
quantity/unit, and the operation actually attempted. Follow standard code and
current data for the relevant version. Start with these tables:

- Work: `WHSWorkTable` (header), `WHSWorkLine` (lines).
- Loads/shipments: `WHSLoadTable`, `WHSLoadLine`, `WHSShipmentTable`.
- Containers/dimensions: `WHSContainerTable`, `InventDim`.
- ASN packing: inspect `WHSUOMStructure` and `WHSASNItem`. In a tested inbound
  fixture, parent/child links were in the packing structure while the LP master
  parent was blank. Verify schema/company and the actual route before concluding
  the hierarchy is broken.
- Inventory: `InventTrans`, `InventTransOrigin`, `InventSum`.

Work can have many pick/put lines; never assume one of each. Inspect the actual
relations and company keys before joining. Link `InventTrans.InventTransOrigin`
to `InventTransOrigin.RecId`; obtain the document reference from the origin,
not a guessed reference column on InventTrans.

Run business processes through their application classes or the mobile app.
A hand-set status does not perform the associated posting, validation or cleanup.

## Process Guide state, errors and retries

A new device request reconstructs framework objects from request/session data.
Do not depend on instance fields across requests. Several steps can execute
within one request: a request boundary is not synonymous with a step boundary.
Use the framework's session/pass-through mechanisms for appropriate transient
state and the proper owner for durable business state.

In the standard WHS mobile route in **10.0.48/10.0.49**,
`WHSFormBuilderRunWithRetry.run` wraps the request in `ttsbegin/ttscommit`,
including validation, execution and building the next page. This covers Process
Guide and classic WHS flows. Step entry is at `appl.ttsLevel() == 1`; never
require zero. Nested code can raise the level. The request, not your helper's
return, owns the commit.

Deadlock, transient connection, update-conflict and duplicate-key failures can
retry the whole request. Make side effects retry-safe. An ordinary thrown error
aborts the transaction and reaches the framework handler; do not rely on a local
catch inside it to navigate to your custom failure page. For that navigation,
return a typed failure before writes. Use framework errors when rollback and
its error page are the intended result; not every validation needs a custom
result type.

Found in the standard source for both versions and confirmed at runtime.
Check the wrapper on another version or a route outside this framework.

If a failure record must survive business rollback, treat independent-connection
logging as a deliberate design choice. Check lock interactions, retries and
duplicates, transaction ownership, cleanup and failure of the logger itself.
Do not independently commit business state or emit an external side effect
merely because an audit record needs to persist.

[Microsoft's Process Guide description](https://learn.microsoft.com/en-us/dynamics365/supply-chain/supply-chain-dev/process-guide-framework)
explains navigation, validation and error handling. Verify installed source
and the actual path for claims beyond that description.

## Running flows: protocol first, the form only to watch

Device tests run through d365whsapp (`whs_login`, `whs_submit`,
`whs_run_scenario`): the app's own protocol, nothing rendered. `whs_log`
shows what a worker did and saw, step by step; use it to report a run.
Every d365whsapp result carries `mode`. `mode: form` means the box runs the
in-form simulator instead of the protocol (its configuration, not your
choice): report the run as a form-mode run and list its `not_checked`.

Only when the person asks to watch ("show me the flow", "let me watch"),
open the in-form simulator with d365web's own tools (`web_open`, `web_set`,
`web_press`):
`/?cmp=<company>&mi=action:WHSWorkExecute`. They see it in Edge when
d365web.json has `"headless": false`; otherwise say so. One tab drives one
worker. Mark what the form shows as a simulator result: it can differ from the
device (context classes behave slightly differently) and never replaces a
d365whsapp run. Never choose the form yourself as the test route.

## Device acceptance

Check the real device route, not just page-builder source:
- Page/step identity and title match the action.
- Default confirmation action is correct; Back/Cancel exit or return correctly.
- Repeated scans, duplicate submissions and retries have intended effects.
- Canonical record identifiers are distinguished from raw scanned input.
- Line captions distinguish the records users must choose between.
- Company, warehouse, dimensions, quantities and user permissions are correct.

These are acceptance questions, not a claim that one particular XML shape or
custom factory is mandatory. Prefer framework defaults when they meet the need;
share required control identifiers instead of duplicating string contracts.

For owner-operated tests, give one action and a visible page title, not an
internal step number. Name the button; do not say "accept the default" without
knowing its effect. In a tested WHS web client, Enter activated the default
and double Enter dismissed the failure page. Ask the operator to pause there;
a database log cannot prove which page text they saw. Distinguish fresh infolog
lines from older output and request a screenshot only when it resolves a gap.

Check persisted effects at meaningful transitions, by relevant item/batch/LP
and quantity/unit. Current InventTrans rows are not a submission history;
unitization and LP reuse can change the apparent grouping. Record operator
observations separately from data and inference; use [practice.md](practice.md)
for verdicts and report-only work.

Test date parsing as well as display: one receipt accepted ISO input despite
an m/d/yyyy user format. Do not infer accepted syntax from the format setting;
check ambiguous day/month input against the intended parsed date. Revalidate
relative-date fixtures when resuming on another day.
