# Warehouse and mobile work

Read with [d365.md](d365.md) when the task involves SCM warehouse processes.

## Trace one real business path

Establish menu item/mode, company, warehouse/user context, document, dimensions,
quantity/unit, and the operation actually attempted. Follow standard code and
current data for the relevant version. Start with these tables:

- Work: `WHSWorkTable` (header), `WHSWorkLine` (lines).
- Loads/shipments: `WHSLoadTable`, `WHSLoadLine`, `WHSShipmentTable`.
- Containers/dimensions: `WHSContainerTable`, `InventDim`.
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

During an owner-operated test, check the relevant persisted effects at meaningful
transitions. Record device observations as owner-reported; ask for exact wording
when it changes the verdict. Keep observed cause, inferred cause, proposed fix
and verified fix separate. The first lesson report contained reported-but-unfixed
page defects, not a generally validated remedy.

Keep pass, fail, blocked, not run and no verdict distinct. If the instruction
is report-only, preserve evidence and report findings without changing code,
setup or data to make the test pass.
