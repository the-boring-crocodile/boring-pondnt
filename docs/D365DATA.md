# d365data: OData and DMF

A separate authenticated HTTP service for public D365 OData entity sets and
DMF packages. It uses its own Entra app, D365 user and MCP credentials.
On the test box, the dedicated app exercised OData and DMF with a limited user
and then with System administrator, the setup used for dev-box regression.
The live checks covered a stress round, the limited user's permissions and
an administrator run.

## Install

### 1. Choose the app, user and D365 permissions

Use a **dedicated Entra app mapped to a dedicated D365 user**. Do not reuse
the warehouse app or the Admin user. Map the app in
D365's Microsoft Entra applications form. The MCP access key authenticates
Copilot to this service, independently of the Entra credentials used for D365.

**Which rights the user gets depends on the box:**

- **Your own dev box (testing, regression):** give the dedicated user
  **System administrator**. Tests then cover everything an administrator can
  do, including creating DMF projects and restarting any batch job.
- **Shared boxes and Tier 2 sandboxes:** give it a limited role, as below.

**The permissions table below is source evidence; the role after it was
verified live.** Choose the
entities, operations and legal entities first. D365's security administrator
then assigns a restricted role with the relevant entity and action privileges.
There is no single role that fits arbitrary OData entities.

The standard 10.0.49 source snapshot 5ff33b1fcfab4161bd37165aff8c1197 shows:

| Use | Standard privilege / source evidence |
|---|---|
| DMF package actions | DataManagementDefinitionGroupEntityMaintainSecurityPrivilege grants Invoke on ExportToPackage, ImportFromPackage, GetExportedPackageUrl, GetExecutionSummaryStatus, GetExecutionErrors and GetAzureWriteUrl |
| Project and mapping entities | DataManagementDefinitionGroupEntityMaintainSecurityPrivilege and DataManagementDefinitionGroupDetailEntityMaintainSecurityPrivilege contain entity Update grants; verify creation/deletion permissions on the target |
| Discover registered DMF entities | DataManagementEntityViewSecurityPrivilege grants Read on DataManagementEntity |
| Read batch jobs | BatchJobEntityView grants Read on BatchJobEntity. The limited-user test below used a custom grant. |
| Example reference-data create/read/delete | EntAssetMaintenanceJobGroupEntityMaintain grants Create, Read, Update, Delete and Correct; the DataManagementEntAssetReferenceEntitiesMaintain duty references it |

These privilege files are under ApplicationFoundation/AxSecurityPrivilege
and, for the example, EnterpriseAssetManagementAppSuite/AxSecurityPrivilege.
They are privilege names, not a verified standard role assignment. Searches
did not establish a complete DMF role/duty chain in this snapshot.
ApplicationFoundation_BPSuppressions.xml:22486 records that
DataManagementDefinitionGroupEntityMaintainSecurityPrivilege is not
referenced by any duty. Even the similarly named View privilege lists
write-capable DMF methods: do not infer permissions from its name.

Verified live on the 10.0.49 test box (2026-11-03): a custom role with one
custom privilege, assigned only to the dedicated user and scoped to one
legal entity, granting Read, Create, Update and Delete on
DataManagementDefinitionGroupEntity and DataManagementDefinitionGroupDetailEntity
and Invoke on ExportToPackage, ImportFromPackage, GetAzureWriteUrl,
GetExportedPackageUrl, GetExecutionSummaryStatus and GetExecutionErrors,
plus the target entity's own privilege. With it, the dedicated user mapped,
exported, downloaded and imported. The standard Maintain privileges grant
Update on those entities but not Create or Delete. Creating a project header
as that user still hangs (below), so create headers in the Data management
workspace.

DMF also needs the target entities' import/export permissions. OData needs
each selected entity's privileges for the requested operations, including
action-specific permissions. Company restrictions belong to D365 security;
the client's company filter is not an authorization boundary.
Verify the final role set, permitted operations and company boundary with
the dedicated user on the target. App, user and role creation belong to
the operator.

### 2. Prepare private folders and the DPAPI secret

Run this once, in elevated PowerShell **as the person whose logon task will
run d365data**. It creates a new private parent and package folder. If the
parent exists, stop and review its contents and permissions rather than
changing them with this new-folder recipe. Setup's ACL helper protects its
release directory only; setup does not create this package folder.

~~~powershell
$ErrorActionPreference = 'Stop'
$root = 'C:\ProgramData\d365data'
if (Test-Path -LiteralPath $root) { throw 'd365data folder exists; review it before continuing' }
New-Item -ItemType Directory -Path $root | Out-Null
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls.exe $root /inheritance:r /grant:r ('*' + $sid + ':(OI)(CI)F') '*S-1-5-18:(OI)(CI)F'
if ($LASTEXITCODE) { throw 'Could not restrict d365data folder permissions' }
New-Item -ItemType Directory -Path "$root\packages" | Out-Null
~~~

Only this person's SID and SYSTEM receive access; the new package folder
inherits it. Keep the parent under the operator's control, without links or
reparse points. The service creates no folders. Store the app secret using
the same DPAPI procedure as [D365WHSAPP](D365WHSAPP.md#2-store-the-client-secret-for-the-service-account):

~~~powershell
Read-Host 'client secret' -AsSecureString | ConvertFrom-SecureString |
    Set-Content C:\ProgramData\d365data\client-secret.dpapi
~~~

DPAPI binds it to this Windows user and machine. Put its path, never its
plaintext, in JSON. Copying the file does not transfer the ability to decrypt it.

### 3. Configure and select the service

Put d365data.json in
%LOCALAPPDATA%\boring-pondnt\services\d365data\d365data.json.
Replace the placeholders with the intended target. Every Config field is shown:

~~~json
{
  "aos_url": "https://aos.example",
  "tenant": "tenant.example",
  "client_id": "<application-client-id>",
  "secret_file": "C:\\ProgramData\\d365data\\client-secret.dpapi",
  "scope": "https://aos.example/.default",
  "company": "<company>",
  "timeout": 60,
  "dmf_submit_timeout": 600,
  "package_folder": "C:\\ProgramData\\d365data\\packages",
  "metadata_max_bytes": 134217728
}
~~~

scope is optional and defaults to aos_url + "/.default"; verify the resource
audience on the target. company is optional and has no built-in default;
use an existing legal entity (at most four characters for DMF).
`timeout` defaults to 60 seconds. `dmf_submit_timeout` defaults to 600
seconds and applies to the background import worker. Both accept positive
finite numbers. The first read after IIS starts can outlast the Pond call
budget.
metadata_max_bytes is optional, defaults to 128 MiB and accepts 1–268435456
bytes (256 MiB). Oversized metadata fails with a `limit` error that names
metadata_max_bytes as the setting to raise.

Add d365data to the **complete intended selection**, preserving your existing
services, then [preview and apply setup](INSTALL.md#the-short-way-setupps1).
For all services with Ferry:

~~~powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -WhatIf -Gateway all -Services 'sourcecache,knowledge,canvas,core,d365,d365web,d365whsapp,d365data'
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Gateway all -Services 'sourcecache,knowledge,canvas,core,d365,d365web,d365whsapp,d365data'
~~~

Select other opt-ins only when their configurations are ready. d365data is
never installed by default; setup refuses it while d365data.json is missing.
Its task runs as you, elevated, at logon through pythonw.exe, like d365whsapp.
The backend listens on loopback 18518; Ferry exposes it through 18517 when
selected for the gateway. It has its own config.json and access key.
Ferry uses your handbook's `data.md` if present. Otherwise it serves the
release's `handbook/data.md`. To adapt it, copy the page into your handbook
and rerun setup to point Ferry to your copy.
Uninstall removes owned tasks and client entries and retains state folders,
the DPAPI secret and package files for the operator to review.

A manual start uses the same two configurations:

~~~text
boring-pondnt-d365data serve --config <service.json> --d365data <d365data.json>
~~~

service.json configures the authenticated HTTP listener and MCP caller roles;
d365data.json configures the D365 target. There is no stdio mode.
The existing Pond tunnel has explicit per-service assignments; it does not
automatically expose d365data.

## DMF packages

DMF uses the standard [package API](https://learn.microsoft.com/en-us/dynamics365/fin-ops-core/dev-itpro/data-entities/data-management-api).
D365 owns projects, mappings, execution and business validation.

- `dmf_export(definition_group, company, package_name?)` (writer) exports
  an existing project with ExportToPackage, an empty executionId and
  reExecute=false. D365 mints the returned `execution_id`.
- `dmf_import(definition_group, package_path, company, execute=true, overwrite=true)`
  (writer) validates a local ZIP, obtains GetAzureWriteUrl, uploads a BlockBlob
  and calls ImportFromPackage. Set overwrite=false for composite entities.
  A process admits one import submission at a time. A second import gets
  `busy` with the active `submission_id`. Coordinate separate service
  instances. An `execution_id` confirms submission. With `execute=false`,
  D365 returns an id and attaches the package to the project without
  creating an execution or batch job. Run the project in D365, or import
  again with `execute=true`. `dmf_status` on such a `submission_id` with
  `download=true` attempts nothing and says so in `download_note`.
- `dmf_status` takes exactly one of `execution_id` and `submission_id`.
  It requires reader. `download=true` requires writer **before any request**.
  Its MCP annotation is write-capable because it can create a file.
  Unknown/NotRun/Executing are nonterminal; Succeeded/PartiallySucceeded/
  Failed/Canceled are terminal. A new, unknown status fails explicitly.
  If an export stays NotRun with no errors, check the D365 user's permissions
  on the project's entities. On the test box, missing Batch groups access produced
  an execution header with no batch job and no errors.
  Errors are read for every status, including Executing. A project without
  mapped fields can stay Executing while its errors ask you to regenerate
  the mapping. GetExecutionErrors failures preserve the status and report
  errors_available=false with errors_code and errors_message; HTTP
  404/405/501 also identifies an unavailable endpoint. Empty error text is
  an empty list. Raw control characters in D365's error JSON are accepted;
  malformed JSON or a non-list returns bounded errors_text, errors_truncated
  and errors_note alongside the status. A cut error list reports how many
  entries fit and suggests raising `max_chars`.
- download=true requests GetExportedPackageUrl only after Succeeded or
  PartiallySucceeded. An empty URL or an ineligible state is reported in
  `download_note`. If obtaining the URL, downloading or saving fails, the
  status and errors stay in the result with `downloaded: false`,
  `download_code` and `download_error`. Successful downloads return
  `downloaded: true`, a new `package_path` and `package_bytes`. Each download
  creates a unique file. Pass the returned `package_path` directly to
  `dmf_import`. The path is inside the configured package folder.

### Poll a slow import

If `dmf_import` returns `state: submitting`, call
`dmf_status(submission_id=...)`. The reply reports one of four states.

| State | Result |
|---|---|
| `submitting` | The worker is still running. Poll again later. |
| `submitted` | Includes `execution_id` and the execution status and errors. If the status lookup fails, `status_error` gives the reason. An attach-only import returns its explanatory note. |
| `failed` | Includes the error code and message, plus `upload_name` and a history note when the upload may remain. |
| `unknown` | The service restarted. Check D365 execution history before importing again. |

Submission outcomes live in the service process. The service records the
last 50 submission ids in its state directory so it can report `unknown`
after a restart. It never retries a submission. For a known execution id,
call `dmf_status(execution_id=...)`. An unknown execution id returns
`not_found`. For job history across executions, use the Data management
workspace in D365.

### Prepare a data project

Use the standard public OData entities, so D365 owns validation and mapping.
First query DataManagementEntities for a registered DMF entity. An entity
being public in OData alone does not make it eligible for DMF. Use equality,
for example `filter="EntityName eq 'Batch groups'"`; `contains()` is not
supported on that entity on the tested version.

Create `DataManagementDefinitionGroups` with `Name`, `Description` and
`OperationType` (Export or Import). For exports, also set
`GenerateDataPackage: Yes`. Create `DataManagementDefinitionGroupDetails`
with `DefinitionGroupId` and `EntityName` (the registered entity's label
from `DataManagementEntities`, e.g. Document types). Use `SourceFormat: CSV`,
`ExecutionUnit: 1`, `LevelInExecutionUnit: 1`, `SequenceInLevel: 1` and
`AutoGenerateMapping: Yes`. Check `ValidationStatus` and errors before
exporting. Use a separate import project for a round trip.

On the test box, deleting a project via `odata_write` on
`DataManagementDefinitionGroups` removed its lines and execution history.
The finished batch job named `Import process: <execution id>` remained.

On the test box, direct project creation by the dedicated app timed out while
D365 waited on the DMF project security cache. Inspect the transaction
before retrying a timed-out create. Creating the project headers in the
Data management workspace allowed the dedicated app to manage mappings
and complete a one-row package round trip against those projects.
The import reused the prepared project with overwrite=true and parallel
package import disabled on that VM.

DMF argument limits follow D365's EDTs: definition_group 60 characters,
package_name 100, execution_id 90 and company 4. Execution ids are opaque
text, including locale-formatted dates, spaces, slashes and colons; keep
them unchanged and never use them as filenames.

### Storage on local development VMs

Since D365's June 2026 security hardening, ImportFromPackage accepts only
HTTPS package URLs with a hostname, not an IP literal. A local development
VM's Azure storage emulator returns `http://127.0.0.1:10000/...`: export,
status, download and blob upload can work, while D365 rejects import.
Import needs storage D365 accepts, such as an Azure storage account;
cloud-hosted, Tier 2 and UDE environments have one. Establish the actual
storage and import behavior on the target before relying on it.

On a local development VM, the operator can point D365 at an Azure storage
account instead: set `AzureStorage.StorageConnectionString` in the AOS
`web.config` and in `bin\Batch.exe.config` to the account's HTTPS connection
string, then restart IIS and the batch service. Back up both files first;
the connection string is a secret, so this is the operator's step, never the
service's. On a 10.0.49 VM this made the full round trip work with D365's URL
check in force (export, download, upload, import: all succeeded).

After a timeout or lost response, inspect the project's execution history
before retrying. An execution may exist without a returned id. An import
failure can leave a blob identified by `upload_name`. The service leaves
the blob available to any queued import.

Configure an existing absolute package_folder for imports and downloads.
Paths must stay inside it, without traversal, symlinks, junctions or other
reparse points. After opening an upload, the service resolves its path again
and refuses it unless it still lies inside the folder and names the opened
file. Uploads must be regular files with one link. Packages are
limited to 64 MiB, transferred as bytes, checked as ZIP files and never
extracted. The transport bounds reads even without Content-Length. This is
an in-memory transfer limit, not a streaming implementation. Keep the folder
and its ancestors private to the service account: path checks do not defend
against another process replacing ancestor directories during an operation.
The service creates no package folders.

Storage URLs come only from D365. Remote storage requires HTTPS. Literal
127.0.0.1 and ::1 over HTTP are accepted for the dev VM's storage emulator
so the service can download its exports. The request stays on the service
host, uses D365's URL unchanged, carries no AOS bearer token and follows
no redirect. The exception applies automatically to those literal hosts.
Local packages and remote storage retention belong to the operator.

## Result size

`odata_entities`, `odata_query`, `odata_write`, `odata_action` and
`dmf_status` accept `max_chars` from 1 to 262,144, default 32,768.
The limit counts compact JSON characters for returned rows, fields,
records, action values or DMF error data. Unicode characters count
directly. Result metadata such as status and continuation
links remains available when the payload is cut. The limit does not change
request bodies or package downloads. Pass `top` and `max_chars` as integers.

OData rows, written records and action values are returned without masking.
OData failures carry the HTTP status and D365's error code, message and
nested messages and types, with stack traces omitted. Diagnostic text masks
query-string URLs, bearer credentials, Authorization values and `sig=`
values. DMF error text also omits URLs without query strings. Entra failures
report the error name, AADSTS error codes and the first description line
without identifiers or timestamps.

The Entra token is cached until two minutes before it expires. When D365
answers HTTP 401, the service discards that token. A read (GET) is repeated
once with a new token; a write, action or DMF call is not repeated, and its
401 failure says that the next call uses a new token.

## Reading

- `odata_entities(search?, entity?, refresh?, max_chars?)` lists matching public entity-set names,
  or shows a set's fields, types and keys. Metadata is cached per process.
  After new entities are built and synchronized, use `refresh=true` to drop and
  reload the cache.
  `company_specific` is inferred from the presence of a `dataAreaId`
  field. Validate this inference for the particular target. A cut list
  explains how to narrow the search or raise `max_chars`.
- `odata_query(entity?, filter?, select?, order_by?, top?, company?, cross_company?, next?, max_chars?, include_etag?, count?)`
  requests pages of up to 20 rows by default (maximum 500), using
  `Prefer: odata.maxpagesize`. An explicit company or config
  default adds a `dataAreaId` filter for company-specific entities and enables
  cross-company querying. `cross_company: true` without a company reads all
  companies (the config default is skipped). The config default does not apply to shared entities;
  an explicit company for a shared entity is refused. Without a company, D365
  answers in the mapped user's default company unless cross-company is enabled.
  An explicit empty company is refused. D365 evaluates the OData filter, select and order_by expressions. Use `eq`
  for string filters. On the test box, `contains()`, `startswith()` and string
  comparisons with `ge` returned HTTP 400 (other operators on strings untested).
  Set `include_etag=true` to return `@odata.etag` on rows. The default omits it.
  Filters with unbalanced parentheses outside quoted strings or unclosed
  string literals are refused before any request. `count=true` requests
  D365's total for the query's filter and company scope. The service returns
  it as `count` before applying local row limits. For totals per company,
  send one counted query per company with `top=1`.
- `next` reports a server-provided continuation link if present. To follow it,
  call `odata_query(next=...)`. You can also pass `top`, `max_chars` and
  `include_etag` on a continuation. Omit `count` (or pass `false`) because
  D365 carries the option in the link; `count=true` is refused.
  If the link contains D365's `$top` page size, the client repeats that size
  in `Prefer`; an explicit `top` must match. If the link has no size, the
  continuation defaults to 500. When a page exceeds the size limit or `top`,
  a `note` reports how many rows fit. A next link continues after the whole
  page. If D365 sends no link and some rows fit, repeat with `top` at or below
  the number shown and follow any new continuation. A narrower `select`
  or larger `max_chars` can help even when no rows fit.
  Links must be under the configured AOS `/data/` prefix and use printable
  ASCII without spaces. Data requests do not follow HTTP redirects. Links
  up to 16,384 characters can be returned and followed. If D365 sends a
  longer link, the service returns the rows with `next: null` and a note.
  `shown` and `truncated` report row cuts. Pages over 32 MiB fail with
  advice to use a smaller `top` or narrower `select`.

Select the fields you need. On the test box, narrow selects ran 5–14× faster.
When you need every field on a wide entity, start with `top` 25–50.
Five-field SalesOrderLines reads reached 398 rows/s versus 69 with all
fields. Five-field journal reads reached 963 rows/s alone versus 254 for the
all-field parallel case.

Batch-job status can be read from `BatchJobs`. In the custom test
privilege, grant Read on `BatchJobEntity` for this. It applies to other
visible jobs as well as the job being tested. Batch job history and tasks
are available through read-only `d365_sql` because the history entity is
not public.

## Writing and actions

`odata_write(entity, op, key?, values?, company?, max_chars?, include_etag?, etag?)` requires the writer role.
`op` is create, update or delete. Update/delete require all metadata key fields;
a selected company supplies a dataAreaId key where applicable. The company
comparison ignores case. Create adds the selected dataAreaId to the
submitted values. Supported keys include `Edm.String`, `Edm.Guid`,
`Edm.Boolean`, `Edm.Byte`, `Edm.SByte`, `Edm.Int16`, `Edm.Int32`,
`Edm.Int64`, `Edm.Date`, `Edm.DateTimeOffset`, `Edm.Decimal` and enum
members. Use `YYYY-MM-DD` for dates, an ISO timestamp with an offset for
`Edm.DateTimeOffset`, a number or plain decimal string for `Edm.Decimal`,
and the member name for enums. The service uses the metadata's qualified
enum type and checks member names when supplied by metadata.
Update and delete refuse string keys containing `/` or `\` because D365
cannot address them in an OData URL. Create accepts these keys and returns
a note to use the D365 client for later updates or deletes.
Set `include_etag=true` to return `@odata.etag` on a record. Pass a record's `@odata.etag` as `etag`
on an update or delete to write only if it is unchanged: a changed record answers `conflict` and
nothing is written. A gateway or server error (HTTP 5xx) after a write says the write may have
completed. `company` on an update or delete needs an entity whose key has `dataAreaId`.
For a company-specific entity whose key lacks `dataAreaId`, the configured company does not
apply to an update or delete: the record is addressed by its key alone and the result's
`note` says so.
Nested values and deep inserts are not supported. D365 can send no body
after an update. The service then returns `record: null` with a note. Read
back the record if the new values matter.

`odata_action(entity, action, arguments?, max_chars?)` calls a collection action as a
writer, for example `BatchJobs.SetBatchJobToWaiting`. DMF's
DataManagementDefinitionGroups actions are refused here and belong to the
separate DMF tools. D365 validates action names and permissions. Changes and
actions can affect real business data; use an appropriate D365 user and
target. A lost or oversized response may follow a successful change: inspect
D365 before retrying. No operation is retried automatically.

`BatchJobs.SetBatchJobToWaiting` re-runs a job. Invoke it on a real job only
when you intend to re-run it. D365 can return HTTP 200 with
`IsSuccess: false`. Check `IsSuccess` and D365's `reponseMessage` field in
the body.

The limited-user test granted Invoke on `SetBatchJobToWaiting` alone,
alongside entity Read. D365 accepted the call but its AOS validation
blocked the restart of an Admin-owned job. Ordinary OData PATCH returned
403. The action still requires D365's job ownership or batch-operator
validation: the job's creator, its **Run by** user or a batch operator may
restart it. With Run by set to the dedicated user, the limited user's
restart succeeded (Waiting, Executing, Finished; twice).

The key syntax follows OData 4.0 primitive literals. D365 documents the
qualified enum form.
[OData 4.0 URL conventions](https://docs.oasis-open.org/odata/odata/v4.0/os/part2-url-conventions/odata-v4.0-os-part2-url-conventions.html)
· [D365 OData](https://learn.microsoft.com/en-us/dynamics365/fin-ops-core/dev-itpro/data-entities/odata#using-enums)

## Limits

Query pages can use up to 32 MiB and metadata up to the configured limit
(128 MiB by default, 256 MiB maximum). Both are held in memory. Any reader
can request `refresh=true`, which reloads metadata under the cache lock
without a separate rate limit. Network read timeouts apply per socket
operation, so an in-flight read can exceed the remaining DMF call budget.
Literal loopback HTTP for the dev VM storage emulator has no separate flag.

The Pond allows about 25 seconds per d365data call. An import call can
return a submission id while its worker continues under its own
`dmf_submit_timeout`. A direct service call waits at most 100 seconds for
that worker. Ferry uses the smaller of the backend's `timeout_seconds`
and the front end's call limit when both apply. A Ferry tool call with
neither limit has no fixed backend read timeout. DMF export and status
with download each use a single call deadline. Token, D365 and storage
requests receive the time remaining for their operation.

## Regression run on a dev box

`tools/d365data_regression.py` (repository only, not in the kit) checks a
dev box's d365data end to end through the gateway, as a Copilot client
would, with the person's `.mcp.json`. Run it with the installed kit's
Python after every install:

```
<kit>\python\python.exe d365data_regression.py --out run.json
```

It covers the tool list and guide, entity search, default, explicit and
cross-company reads, paging, the cut note, the local refusals, a
create/update/delete cycle on Document types, enum and decimal keys
(Name affixes, Address format lines) with text returned unmasked, a DMF export, download and
re-import through two new projects, and a batch restart. Everything it
creates starts with ZZREG and is deleted again. The batch check needs one
fixture job named `ZZREG d365data regression (no tasks)`, created once in
the Batch jobs form (BatchJobs is read-only in OData); without it the check
is skipped. It assumes the dev-box setup (D48: the d365data user is an
administrator). Exit status 0 means no check failed.
