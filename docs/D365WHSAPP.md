# d365whsapp: warehouse-flow tests

Drive a warehouse flow without a handheld device: log a test worker in, enter
fields, press a button and check the next page. The service changes real data
in the connected D365 environment; use development or test fixtures.

Choose the backend in `d365whsapp.json`. The service never switches modes
per call or silently falls back when one fails.

| Mode | What it exercises | What it needs |
|---|---|---|
| `app` (default) | The mobile-app service protocol | An Entra app, secret and D365 test user |
| `form` | The in-form warehouse simulator in the web client | A dedicated signed-in Edge profile |

Both expose the same tools, so a scenario can express the same intended flow.
They are different test surfaces: a form-mode pass does not establish protocol
acceptance, and neither mode tests the physical device or Microsoft's mobile
app UI. Successful tool results carry `mode`; refusals/errors may not. Read
the service description to establish the configured backend even before login.

Use [Setup](#setup) for app mode, or go directly to
[Mode form](#mode-form-the-in-form-simulator). For a pending operation, use
[Waiting for a result](#waiting-for-a-result).

## Connection and server startup

The MCP connection is **authenticated HTTP only; stdio is not supported**,
for both app and form mode. The recommended
[pondNT setup](INSTALL.md#the-short-way-setupps1) carries this backend through
Ferry: d365whsapp normally listens on loopback port 18515, and Copilot reaches
it through Ferry on 18517. Core and Knowledge remain direct.

For a manual server start, pass both configuration files:

```text
boring-pondnt-d365whsapp serve --config <service.json> --d365whsapp <d365whsapp.json>
```

`service.json` configures the MCP listener and caller authentication;
`d365whsapp.json` selects and configures the D365 backend. App mode's Entra
credentials and form mode's Edge profile belong to that backend configuration;
neither replaces authentication on the MCP listener. Setup normally provides
the listener configuration and starts the service; an existing manual launcher
can start the same server without Task Scheduler.

## Tools

Tools are writer-only unless noted:

| Tool | What it does |
|---|---|
| `whs_login` | Log a test worker in. An existing server session resumes at its last page (`resumed`). A failed login is never retried: repeated failures disable the worker. |
| `whs_screen` (reader) | App mode: the last returned page, without a server call. Form mode: first collect an outstanding single-step result/error for this worker; otherwise read the form. |
| `whs_log` (reader) | The worker's readable step log, newest last (`lines`, default 60): what was set and pressed, and the page that came back. |
| `whs_submit` | Fill enabled fields and press one button; returns the next page. Read-only or unknown fields are refused before anything is sent. |
| `whs_logoff` | Cancel/Back/Leave to the main menu and log off. |
| `whs_run_scenario` | A list of steps (`values`, `press`, `expect`); one line per step, stops at the first mismatch. A run that outlasts the call (the Pond's 30 s) answers `status: running` with `steps_done`; the worker is busy until `whs_wait` collects it. |
| `whs_wait` | The result of a running `whs_run_scenario` (or `running` again); each result is collected once (calls waiting for the same run at the same moment all get it; a newer run is never touched). While `whs_login`/`whs_submit`/`whs_logoff` runs for a worker, a scenario for it is refused as `busy`. |

A page comes back compact: `page` (Login, Menu, Default, ...), `title`,
`step` (`id`, `title`), `message` (`kind` error or info, `text`), and either
`buttons` (menus) or `controls` (name, type, label, data, readonly, error).

Scenario admission checks the complete list before contacting either backend: values/expectation
maps, scalar field values, text press/note/expectation entries and all supported keys. Malformed
later steps and unknown variables are refused before earlier steps can act. Backend-dependent
availability and application responses remain runtime checks and are reported in the transcript.

## Waiting for a result

Use the collector that owns the outstanding operation:

| Observation | Collector |
|---|---|
| A scenario returns `status: running` in either mode | `whs_wait(worker=...)`, until the run returns its result |
| A single form-mode operation reports `step_running` | `whs_screen(worker=...)` for that same worker |
| An app-mode reply reports `recovered: true` | Inspect the resumed page before proceeding; recovery did not replay the step |

These are different lifecycles. `whs_wait` collects scenarios; it does not
collect a standalone form step. Pending results retain ownership until collected
so a later call cannot overwrite evidence of an earlier action. A lost reply
does not establish that no receipt, work or other business effect occurred.

## The step log

Every login, submit, logoff and scenario step appends an entry to a plain-text
file per worker, `<state>/whs-steps/steps-<WORKER>.log` (past 1 MiB it moves
to `.log.1`; one old file is kept). It shows what the app "saw", not how the
Microsoft app draws it:

```text
2026-10-26 09:20:08  WHSAPP01  press OK, values {[PONum]: 00000050}
  -> Default "Purchase Receive" step PONum "Enter purchase order number"
     Location [WMSLocationId]: RECV (read-only)
     [PONum]: (error)
     buttons: OK, Cancel
     message error: Product cannot be received.
       Please contact your supervisor.
```

The login's password is never written; values of password fields show as
`***`. A failed call is logged with its error code. If the log cannot be
written, the step still runs and its result carries `log_error` (once until a
write succeeds again). `whs_log` reads the tail.

## How the protocol behaves (read before writing tests)

App mode calls `WHSMobileAppServices/WHSMobileAppService/getNextFormHandHeld`.

- **One session per worker.** D365 keys the session by worker. Parallel test
  runs need one worker each. Each worker gets a fixed device id.
- **The page is echoed.** The client sends back the page it received with
  only editable fields changed; anything else is rejected as
  `REQUEST_XML_TAMPERING` (the session survives it).
- **Messages** come in the page, not as errors: "Product cannot be received"
  is `message.kind = error`, "Work Completed" is `info`.
- **Lost replies.** If a step's reply is lost (timeout) or the server rejects
  the session token, d365whsapp logs in again and the server resumes at its last
  processed page; the result says `recovered: true`. Check that page: the step
  may or may not have run. d365whsapp never resends a step blindly.
- **Purchase orders** must be Approved or Confirmed to be received (or "In
  external review" with the warehouse parameter that allows it).

The protocol and form backends have separate acceptance limits. Validate the
actual receiving routes on the target environment before relying on either.

## Setup

This section configures **app mode**. Form mode uses the separate browser
setup below and does not need this service's client secret. In either mode,
choose the intended environment and an existing company explicitly; USMF is
a Contoso example, not an installation default.

### 1. Confirm the environment and token audience

A fresh downloadable VHD first needs its AOS sign-in provisioned. Follow
[Microsoft's VHD first-use procedure](https://learn.microsoft.com/en-us/dynamics365/fin-ops-core/dev-itpro/dev-tools/vhd-setup)
for its application registration, certificate setup and administrator
provisioning. That prepares the environment itself; it is not a step to repeat
on an already-working cloud environment to add this test client.

App mode currently uses `client_credentials` at the tenant's v2 token endpoint
with **`scope=<client_id>/.default`**. There is no separate audience/resource
setting. The original VHD setup used an application ID that the AOS accepted
as its token audience. Before using another environment, establish that its
authentication configuration accepts this request. An arbitrary new client
registration or an existing cloud sign-in application is not sufficient proof.

Microsoft's [service-endpoint guide](https://learn.microsoft.com/en-us/dynamics365/fin-ops-core/dev-itpro/data-entities/services-home-page)
describes service-to-service authentication and mapping the client ID to a D365
service user. Use a user with the permissions needed for the intended test.
The guide does not establish that this client's fixed audience choice fits
every deployment.

This is the emulator's authentication path. For Microsoft's actual mobile app,
follow its [current authentication guidance](https://learn.microsoft.com/en-us/dynamics365/supply-chain/warehousing/warehouse-app-user-based-auth-faq):
service-based mobile-app authentication is deprecated there. Protocol and
device sign-in acceptance remain separate.

### 2. Store the client secret for the service account

Once the environment's administrator has confirmed the intended registration
and D365 user mapping, create a client secret for that registration. On the
Windows machine, run this as the account that will run d365whsapp:

```powershell
New-Item -ItemType Directory -Force C:\ProgramData\d365whsapp | Out-Null
Read-Host 'client secret' -AsSecureString | ConvertFrom-SecureString |
    Set-Content C:\ProgramData\d365whsapp\client-secret.dpapi
```

The file is encrypted with Windows DPAPI for that user and machine. Copying it
to another machine or running the service as another account does not transfer
the ability to decrypt it. Keep the file private and put its path, not its
plaintext secret, in `d365whsapp.json`. Provisioning this secret does not test
the token audience or the warehouse route.

### 3. Test workers

One worker per parallel run. In USMF on the demo data: Warehouse management →
Setup → Worker → a worker → Users → New (user id, default warehouse, menu),
then **Reset password**. The password is stored encrypted per record; it
cannot be copied between users in the database.

### 4. d365whsapp.json

```json
{
  "aos_url": "https://usnconeboxax1aos.cloud.onebox.dynamics.com",
  "tenant": "<your-tenant>.onmicrosoft.com",
  "client_id": "<application (client) id>",
  "secret_file": "C:\\ProgramData\\d365whsapp\\client-secret.dpapi",
  "company": "usmf",
  "step_timeout": 300
}
```

`company` is optional: there is no built-in one. Without it every `whs_login` must name its company
(otherwise `no_company`, refused before anything reaches D365).

- **For Copilot on the machine:** put it in
  `%LOCALAPPDATA%\boring-pondnt\services\d365whsapp\d365whsapp.json`, then
  [preview and apply pondNT setup](INSTALL.md) with `d365whsapp` included in your
  complete intended `-Services` list and `-Gateway all`. This keeps core and
  Knowledge direct and carries the other selected services through Ferry.
  Setup refuses d365whsapp while the file is missing.
- **For a Pond through the tunnel:** put it in the tunnel's folder
  (`C:\temp\ci\d365whsapp.json`) next to the provisioned `devbox-d365whsapp-id` and
  `devbox-d365whsapp-secret`. `devbox-start.ps1` then starts `devbox-d365whsapp` on
  18524 (the tunnel account must permit listening on 18524).

The first OData or service call after an IIS start can take minutes (warm-up);
`step_timeout` covers heavy steps such as a receipt that creates work.

## Mode form: the in-form simulator

`/?cmp=<company>&mi=action:WHSWorkExecute` shows the warehouse app inside the web client ("only intended
for testing purposes"). It uses the signed-in browser session, so tests can
exercise the form workflow without provisioning the protocol client's secret.
Use app mode when the protocol is part of the acceptance requirement. Context
classes can differ between the two paths; confirm the relevant receiving route
in that mode rather than treating one pass as coverage of both.

```json
{
  "mode": "form",
  "form": {"url": "https://usnconeboxax1aos.cloud.onebox.dynamics.com", "company": "usmf",
           "profile": "d365whsapp-form", "debug_port": 9334, "headless": true}
}
```

The `form` block takes [d365web.json's keys](D365WEB.md#setup) and drives its own Edge through d365web's
library, separate from the d365web server's browser: give it its **own profile** (default
`d365whsapp-form`, under `%LOCALAPPDATA%\boring-pondnt\d365web\profiles`; Edge runs one process per
profile) and its own `debug_port`. Sign that profile in using
[d365web's setup procedure](D365WEB.md#setup), under the account that runs
d365whsapp. When `profiles_dir` is set, it replaces the complete profile path;
give each service a different directory. App keys (`aos_url`, `tenant`, ...)
may stay in the file; only `mode` decides.

- One tab per worker, next to an untouched main tab. One browser means one
  step at a time: calls queue, workers take turns step by step.
- What the form cannot show is listed in the scenario result's `not_checked`, never passed: the
  pattern of work pages, a `step` without a control named after it (a custom flow that names its labels
  after the step has its steps checked), and `message_kind` (the form paints info and error alike).
- A page comes back as `page` (Login, Menu or `work`), `title`, `message` (`text` only), `buttons`,
  `fields`, `readonly`, `labels`. `resumed` is true when the login lands on a work page (D365 resumed
  the worker's session); `recovered` does not occur.
- A single step that outlasts the call answers `step_running`. Call `whs_screen` for that worker to
  collect the original result or error; while it is outstanding, other actions answer `busy` and
  cannot consume it. After collection, `whs_screen` reads the current form again. An error may leave
  the operation's outcome unknown: inspect the page before deciding what to do; it is not retried.
  Scenario steps wait in the run's own thread (up to the form block's `step_timeout`).
- Submit and login stop before pressing if field setting stops, readback is unavailable, or a value
  differs under the browser client's existing comparison rules. `set_incomplete` carries the applied
  values, any `stopped_at`/new messages and the form page; secret field values are masked.
- The step log is written as in app mode; form pages are marked `(form)`.

## Scenario variables and label expectations

`whs_run_scenario` takes `variables` (`${name}` in values, press and expect; e.g. a PO number that a
d365web scenario saved) and per-step `note`s. Expectations also take `labels` {control: substring of its
label} for label controls such as S12's `DemoExceptionReason` ("Line locked"). `whs_logoff` leaves
custom scan pages through "Leave document". Example across both services: `tests/scenarios/`.
