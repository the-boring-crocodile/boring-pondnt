# What has been tested where

pondNT is built on Linux and runs on Windows next to D365, so "the tests
pass" can mean very different things. This page says which kind of
evidence was recorded for particular builds, and what remains unverified.
Passing an older build does not accept every later candidate carrying the
same version number. A row that is not here is untested.

## Kinds of evidence

| Kind | What it shows | What it does not |
|---|---|---|
| **Portable CI** (GitHub Actions, Ubuntu 24.04, Python 3.12, 3.14) | the code and its tests on Linux; Windows-only tests skip themselves | anything about Windows or D365 |
| **Native suite** (the release zip's `run-tests.cmd` and `run-services-tests.cmd` on a Windows D365 box) | processes, encodings, locks, the installer journey and the bundled services on real Windows | live D365 behaviour: the D365 tests there use recorded fixtures |
| **Live on a dev box** | the tools against a running D365: builds, xref, SQL, browser forms, warehouse flows | another D365 version, topology or customisation |
| **Reported use** | someone used a release for real work and reports no problem | what exactly was exercised |

## Where

| Environment | Recorded Windows target | Recorded D365 target | Scope of evidence |
|---|---|---|---|
| Local Hyper-V dev VM (VHD), our test box | Windows Server 2022 Datacenter, build 20348; Windows PowerShell 5.1; SQL Server 2019 (15.0.4480); Visual Studio 2022 17.14 | application 10.0.49, platform Update 73 (7.0.8199.32), Contoso demo data | native suite with Edge: 1301 tests, 45 skipped, 0 failures; bundled services: 532, 2 skipped, 0 failures; live: xref, SQL read-back, browser forms with both d365web backends, warehouse form mode, gateway routing ([first check](D365WEB.md#from-nothing-to-the-first-check-in-one-go)) |
| A second dev VM, a 10.0.49 one-box | Windows Server (version not recorded here) | application 10.0.49, demo data | native suite and bundled services as above; installer journey and 0.2.x release-folder lockdown; install as an update over an earlier candidate; live: browser forms with both backends, in a visible window and headless; warehouse form and app modes; a reboot (see below) |
| Azure cloud-hosted dev VM, used for client work | Windows Server (version not recorded here) | D365 client build 7.0.26176.1; application version not recorded here | 0.2.10's last candidate installed as an update over an earlier 0.2.10 development build ([step 10](INSTALL.md#10-update-and-back)); every D365 client viewmodel name the model backend uses present; a read-only d365web check through Copilot in Visual Studio (list, quick filter, row selection, a dialog opened and cancelled, all through the model backend); earlier, reported use of 0.2.7 |

The native target uses the release's bundled Python 3.14 and PowerShell 7;
Copilot in Visual Studio is the recorded client. The test counts belong to
0.2.10's last candidate (see below); 0.2.9 recorded 696 and 515.

After a reboot of the second dev VM, while D365 was still starting,
`whs_logoff` answered `retry_later` twice (nothing pressed, the session
kept) and then logged off about 70 seconds after boot. The first `web_open`
after that reboot failed with "Edge exited with 0 (profile in use?)"; the
second attached normally. That has not been rechecked after another reboot.

### Recorded artifact results

The retained acceptance records identify these builds. Their sanitized
identifiers are repeated here so a public reader can compare an artifact
without access to the operators' private run directories. The 0.2.x
installer could lock down the release folder. Starting with 0.3.0, setup
warns about possible write access or unchecked paths and leaves ACLs alone.

| Build | Native result | Artifact identity |
|---|---|---|
| 0.2.9, source `d583c0a` | 696 native tests and 515 bundled-service tests, no failures; installer, 0.2.x lockdown and cleanup passed | release ZIP SHA-256 `310110347e301bec65ec6f9a6e2b173f16520bab86322bc096741556dac66a4f`; tests ZIP `2c6cd78ea53d58c3b4610c465b3641cf4c1d4177919b7fdac8953296084474a1` |
| 0.2.10's last candidate (0.2.10rc17; the release differs only in version numbers) | 1301 native tests (45 skipped) and 532 bundled-service tests (2 skipped), no failures, on both 10.0.49 dev VMs; installer, 0.2.x lockdown and cleanup passed on the second | release ZIP `f3053d6d06fcd7076e4e3b91b4b516e139782cc3fa7289f012ee500e38fdcdd0`; tests ZIP `78bd2e59f4b10330d1081c759160774d9c0298c2e4504bd852c5a7a22e135079` |
| 0.3.0rc4, source `0612de1` | Live migration, desktop tunnel, setup rerun, port change and logon checks on the second dev VM (below); long sleep pending | release ZIP SHA-256 prefix `efdd8de8` |
| Historical 0.3.0 build, source `7df6259` | Accepted on the second dev VM, 2026-11-16 00:41 (Europe/Berlin): rc7 → final update, eight logon tasks and required self-checks, desktop tunnel, retained D365 sign-in and a Pond command; one availability job on each of two Pirates from the host Pond (below) | `boring-pondnt-0.3.0-win-x64.zip`, SHA-256 `35273b586c0358869e52803751223d6616f39f774c4aef257819d112e120105b` |

The published 0.3.0 ZIP will be a new build. Its file will differ from the
historical 0.3.0 archive above.

### Windows upgrade and native replay, 2026-11-19

The upgrade run took place at 00:55–01:05 on the second 10.0.49 development VM.
The native suite was replayed there at 08:50–09:05 (Europe/Berlin).

| Build and check | Recorded result |
|---|---|
| Published 0.2.10 → 0.3.0rc13 (source `13de63e`), legacy SYSTEM/cmd branch | Passed. The saved service selection drove the upgrade and its fixed Ferry routing. Existing keys and client entries were retained. |
| 0.3.0rc13 → published 0.2.10, way back | Passed. Restored the old configuration and client entries, imported the recorded task XML, checked the old tasks and made an authenticated gateway read. |
| 0.3.0rc11 person/logon → 0.3.0rc13 | Passed. The normal update kept the configured services and their keys. |
| 0.3.0rc15 native launchers (source `9e98865`) | `run-tests.cmd` passed 1,469 tests with 52 skips. `run-services-tests.cmd` passed 563 tests with 2 skips and 71 subtests. Both exited 0. Tests ZIP SHA-256 `6262127eb1feb67671354897188369eeffb96f534151362313585d1f74aa47c8`. |
| Sourcecache and `-Index` | Untested natively in these runs. The VM had no Sourcecache index. |
| Company test box, 2026-11-19 | An AI agent followed the published-format pages to upgrade the 0.2.0rc40 mixed layout to 0.3.0rc17 on the legacy branch using `-Services` and `-Index`. Eight logon tasks, self-check, and reads through Ferry and direct, including Sourcecache, passed. |

The [0.2.9 release page](https://github.com/the-boring-crocodile/boring-pondnt/releases/tag/v0.2.9)
is the public release location; this documentation round did not verify its
current attachments. 0.2.10 (Pond Common 0.7.13, the bundled service versions in the CHANGELOG) is the
last candidate with only version numbers changed, its own and the bundled
services' (and MCP Ferry now reporting its own). The published 0.2.10 zip is
a new build; its hashes are on its release page, not here. See
[CHANGELOG](../CHANGELOG.md) and the
[browser first check](D365WEB.md#from-nothing-to-the-first-check-in-one-go)
for the recorded live workflow's scope.

The retired 0.3.0rc1 launcher was observed with hidden SSH and an ensure-task
reconnect after sleep. Its detached process cleanup, Windows owner checks and
`-Only` over SSH were not established. That launcher and its task/record model
have since been removed; those observations remain historical evidence only.

### 0.3.0rc4: migration and desktop tunnel, 2026-11-13

Live on the second 10.0.49 dev VM, source `0612de1`, release ZIP SHA-256
prefix `efdd8de8`. Wald worked on the desktop; the Crocodile checked over
SSH and through the Pond. Times below are UTC. These are live operating
observations, not another run of the full native fixture suite.

- Migration from rc3 under `C:\tools` to rc4 under `C:\Program Files`
  completed around 10:36. Every key was kept; eight tasks run at logon:
  Limited for Canvas, gateway and Knowledge, Highest for the other five.
- The standalone tunnel to a dedicated host account formed one tree:
  cmd → Python → five servers plus SSH. Core, d365, d365data and d365web
  each answered one read-only call; d365web needed one Edge sign-in.
  d365whsapp answered a clean `no_session` refusal.
- SSH was killed at 11:57:03 and replaced at 11:57:09, with unchanged server
  PIDs. A short sleep of about 18 seconds did not drop SSH.
- Closing the window left no tunnel processes in each of three cases:
  with Edge running, idle, and during a core job with cmd → PING.
  Background services stayed running.
- Edge closes after `idle_minutes` and opens again on the next call;
  its sign-in remains in the tunnel profile.
- Setup rerun left all 28 service files byte-identical, including mtimes,
  reported “key kept”, and left `.mcp.json` unchanged.
- Changing d365data's configured port and URL from 18518 to 18519, then
  running setup, kept `config.json` intact, moved the listener and updated
  the gateway's `ferry.json`. A real MCP call through the gateway reached
  d365data. Reverting the same way restored the morning file hashes.
- Sign-out/sign-in at 12:10 started the eight logon tasks (processes present
  about 40 seconds later); the tunnel did not start.
- An example SSH target is `tunnel-devbox@YOUR-HOST`, a
  placeholder. The actual tunnel target existed only in private
  `%LOCALAPPDATA%\pond-tunnel\settings.json`.
- **Pending: sleep longer than the keepalive window (overnight). Result not
  yet reported.**

### Historical 0.3.0 build: update and slim live check, 2026-11-16

The accepted final ZIP above was rehashed on the host and the second Windows
10.0.49 dev VM. It was unpacked beside rc7, previewed with the complete installed
selection and applied in the existing elevated desktop session; setup exited 0
and kept every key, config, gateway choice and adopted handbook. All eight
standard tasks selected the final folder. Core self-check passed all eight
required checks; an optional Defender query still reported "Invalid class",
as before.

The final desktop tunnel reached the host Pond. The retained Edge profile
opened the authenticated D365 dashboard; a command through the Pond returned
its exact marker with the Windows identity and desktop session, exit 0.
One job on each of two Pirates from the host Pond completed with exact markers,
no tools and no writes. These establish Pirate availability; they
are not separate Windows-client Pirate jobs.

The retained acceptance receipt identifies the sanitized final-checks record
by SHA-256 `e09b334b3345104ee007153d6f3ea332aea01333f5e84e0f77e6e82b4124be0f`.
This was a slim native update/live check, not another full native fixture
suite or a rollback test. No SSH-drop retest or long-sleep test was run;
rc7 → final changed only version/pin/floor metadata and the dated changelog.
It does not establish native acceptance of later code changes.

## Per surface

| Surface | Status |
|---|---|
| Core tools (files, exec, jobs, batch) | native suite; live on both 10.0.49 dev VMs |
| Standalone desktop tunnel and simplified setup (0.3.0rc4) | Linux synthetic checks cover configuration, ports, stable-key repair, person/logon task definitions, warning-only permission inspection and packaged entry points. Live migration, independent window startup, nested job/Edge cleanup, SSH reconnect, setup rerun, port changes and logon tasks were observed on the second dev VM (above). **Pending:** sleep beyond the keepalive window; these observations do not exercise every Windows owner/DACL path. Earlier hidden-launcher evidence remains historical. |
| d365: build, results, sync, best practice, `d365_run` | native suite with fixtures; build, results and AOS restarts live on the test box; syncs run live there during development |
| d365: `xref` | live on the test box (DYNAMICSXREFDB, 5.3 million names) |
| d365: `d365_sql` | live on the test box |
| d365web (browser forms) | native suite with Edge; live regression on both 10.0.49 dev VMs with the `model` and `input` backends: standard Contoso forms, an inventory movement journal, combos, lookups, quick filter, grid paging, dialogs, row selection and what moved after a press; the model backend's client names present on a second platform build (client 7.0.26176.1) |
| d365data: OData read, write, actions | Linux suite with fakes; live on the test box with an administrator-mapped app and with a dedicated app mapped to a limited user (its own role, one company): metadata, queries with paging, create/update/delete of a test record. With the USMF-scoped limited user, creating a record in DEMF returned HTTP 403 and the DEMF read returned zero rows. [Data setup and permissions](D365DATA.md) |
| d365data: DMF export, import, status, download | Linux suite with fakes; live on the test box: export, download and import of a one-entity package, a failed import with real error rows, the import with D365's URL check in force against an Azure storage account, and a round trip as the limited user on projects created in the Data management workspace. Limited-user project creation was attempted and stalled in D365. Tier 2 and UDE remain untested. [Setup and live behaviour](D365DATA.md) |
| d365whsapp, `form` mode (in-browser simulator) | native suite; live on both 10.0.49 dev VMs with both d365web backends: login, a wrong password, logoff from eight kinds of page, item inquiry, status change and change-warehouse flows; earlier, a receiving suite of 12 cases (purchase, transfer and ASN receipts, each good and bad) passed in form mode on a development box, as reported to us |
| d365whsapp, `app` mode (the mobile app protocol) | native suite with fixtures; live login and logoff on the second dev VM, through the devbox tunnel; flows beyond that **unverified** in app mode |
| The physical device or Microsoft's mobile app | **not tested**; neither mode exercises them |
| Unified developer experience (UDE) | **not tested** |
| Tier 2 or other sandbox environments | **not tested** (d365data included) |
| D365 versions other than 10.0.49 | **not tested**, apart from the Azure dev VM checks above |
| Windows 10/11 client machines | **not tested**; the targets are Windows Server dev boxes |
| Visual Studio 2022 17.14 (Copilot, Agent mode) | the tested client |
| VS Code and Claude Code | **untested vendor-documented recipes** ([CLIENTS.md](CLIENTS.md)) |
| Claude Desktop | **no verified direct HTTP/bearer recipe** ([CLIENTS.md](CLIENTS.md)) |

## Efficiency

Whether the specialist tools make an agent cheaper has not been measured
properly: that needs a real scenario, repeated runs and a fair comparison.
