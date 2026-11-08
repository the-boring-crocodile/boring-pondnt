# D365 data

`d365data` reads and changes D365 data through OData queries, writes,
entity-set actions and DMF packages. The target and its mapped D365 user
own permissions. D365 data is returned as is (unmasked).

Use a dedicated Entra app and D365 user. Use System administrator for
regression on your own dev box and a limited role on shared boxes.
[Setup](../docs/D365DATA.md#install) · [Live evidence](../docs/EVIDENCE.md#per-surface)

The OData tools and `dmf_status` accept `max_chars` from 1 to 262,144
(default 32,768) for rows, fields, records, action values or DMF errors.
Status and continuation fields remain available when the payload is cut.

## OData

- `odata_entities(search?, entity?, refresh?, max_chars?)` lists sets or
  shows fields, types and keys. Reload after a build and sync with `refresh=true`.
- `odata_query(entity?, filter?, select?, order_by?, top?, company?, cross_company?, next?, max_chars?, include_etag?, count?)`
  reads rows. `count=true` returns the total matching rows from D365 before
  the page is cut. For per-company totals, use one query per company with
  `count=true` and `top=1`. Page size is 1–500, initially 20. Use `eq` for string
  filters on the test box, where `contains()`, `startswith()` and `ge` failed.
  Select only the fields you need and start at `top` 25–50 on wide entities.
- Follow D365's link with `odata_query(next=...)` and optional `top`,
  `max_chars` or `include_etag`. The link carries `$count` when requested.
  A continuation repeats the link's page
  size when present, otherwise defaults to 500. The next link starts
  after the whole page. If rows were cut, repeat with a narrower
  `select`, a smaller `top`, or a larger `max_chars` before following it.
- `odata_write(entity, op, key?, values?, company?, max_chars?, include_etag?)`
  requires writer. Create takes values. Update and delete take all key
  fields, with company supplied from the selected company where applicable.
  When D365 sends no update body, the result is `record: null` with a note.
  Read back if the new values matter. Key types are strings, GUIDs, integers, booleans, dates (`YYYY-MM-DD`),
  timestamps with an offset (`2026-11-04T00:00:00Z`), plain decimals and
  enum member names. A string key containing `/` or `\` can be created
  but must be changed or deleted in the D365 client.
- `odata_action(entity, action, arguments?, max_chars?)` requires writer.
  Use the DMF tools for package actions. For `BatchJobs.SetBatchJobToWaiting`
  inspect `IsSuccess` and D365's `reponseMessage` even on HTTP 200.
  Invoke only when you intend a re-run. D365 checks job ownership or
  batch-operator rights.

On a company-specific set, query filters by the configured company unless
you pass an explicit `company` or pass `cross_company=true` without one
to read all companies the D365 user can see. With neither a configured nor
an explicit company, D365 uses the user's default company unless cross-company
is enabled. Shared sets ignore the configured company and refuse an explicit
one. Omit `company` when unused. Query and write omit `@odata.etag` unless
`include_etag=true`.

## DMF

- `dmf_export(definition_group, company, package_name?)` exports an existing
  project and returns an execution id. Requires writer.
- `dmf_import(definition_group, package_path, company, execute=true, overwrite=true)`
  submits a ZIP of at most 64 MiB. Composite packages require `overwrite=false`.
  Requires writer. If the reply says `state: submitting`, poll
  `dmf_status(submission_id=...)`. With `execute=false`, D365 attaches the
  package. Run the project in D365 or import with `execute=true` to execute it.
- `dmf_status` takes exactly one of `execution_id` and `submission_id`.
  It reads status and errors even while Executing. For a completed export,
  `download=true` requires writer. Use the returned `package_path` inside
  the configured package folder as the input to `dmf_import`. Download
  failures retain status and errors.

To create a project, create a `DataManagementDefinitionGroups` record with
`Name`, `Description` and `OperationType` (Export or Import). For exports
also set `GenerateDataPackage: Yes`. Create the line in
`DataManagementDefinitionGroupDetails` with `DefinitionGroupId`,
`EntityName` (the label from `DataManagementEntities`, e.g. Document types),
`SourceFormat: CSV`, `ExecutionUnit: 1`, `LevelInExecutionUnit: 1`,
`SequenceInLevel: 1` and `AutoGenerateMapping: Yes`.

Delete the project with `odata_write(op="delete")` on
`DataManagementDefinitionGroups`. On the test box this removed the lines and
execution history. The finished batch job stayed. For a known execution,
use `dmf_status(execution_id=...)`. In D365, open job history in the Data
management workspace. Check history before retrying an uncertain import.
A failed import can leave a blob identified by `upload_name`.
[DMF packages](../docs/D365DATA.md#dmf-packages)
