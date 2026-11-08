# What has been tested where

pondNT is built on Linux and runs on Windows next to D365, so "the tests
pass" can mean very different things. This page says which kind of
evidence was recorded for particular builds, and what remains unverified.
Passing an older build does not accept every later candidate carrying the
same version number. A row that is not here is untested.

## Kinds of evidence

| Kind | What it shows | What it does not |
|---|---|---|
| **Portable CI** (GitHub Actions, Ubuntu 24.04, Python 3.10, 3.12, 3.14) | the code and its tests on Linux; Windows-only tests skip themselves | anything about Windows or D365 |
| **Native suite** (the release zip's `run-tests.cmd` and `run-services-tests.cmd` on a Windows D365 box) | processes, encodings, locks, the installer journey and the bundled services on real Windows | live D365 behaviour: the D365 tests there use recorded fixtures |
| **Live on a dev box** | the tools against a running D365: builds, xref, SQL, browser forms, warehouse flows | another D365 version, topology or customisation |
| **Reported use** | someone used a release for real work and reports no problem | what exactly was exercised |

## Where

| Environment | Recorded Windows target | Recorded D365 target | Scope of evidence |
|---|---|---|---|
| Local Hyper-V dev VM (VHD), our test box | Windows Server 2022 Datacenter, build 20348; Windows PowerShell 5.1; SQL Server 2019 (15.0.4480); Visual Studio 2022 17.14 | application 10.0.49, platform Update 73 (7.0.8199.32), Contoso demo data | native suite with Edge: 1301 tests, 45 skipped, 0 failures; bundled services: 532, 2 skipped, 0 failures; live: xref, SQL read-back, browser forms with both d365web backends, warehouse form mode, gateway routing ([first check](D365WEB.md#from-nothing-to-the-first-check-in-one-go)) |
| A second dev VM, a 10.0.49 one-box | Windows Server (version not recorded here) | application 10.0.49, demo data | native suite and bundled services as above; installer journey and release-folder lockdown; install as an update over an earlier candidate; live: browser forms with both backends, in a visible window and headless; warehouse form and app modes; a reboot (see below) |
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
without access to the operators' private run directories:

| Build | Native result | Artifact identity |
|---|---|---|
| 0.2.9, source `d583c0a` | 696 native tests and 515 bundled-service tests, no failures; installer/lockdown/cleanup passed | release ZIP SHA-256 `310110347e301bec65ec6f9a6e2b173f16520bab86322bc096741556dac66a4f`; tests ZIP `2c6cd78ea53d58c3b4610c465b3641cf4c1d4177919b7fdac8953296084474a1` |
| 0.2.10's last candidate (0.2.10rc17; the release differs only in version numbers) | 1301 native tests (45 skipped) and 532 bundled-service tests (2 skipped), no failures, on both 10.0.49 dev VMs; installer/lockdown/cleanup passed on the second | release ZIP `f3053d6d06fcd7076e4e3b91b4b516e139782cc3fa7289f012ee500e38fdcdd0`; tests ZIP `78bd2e59f4b10330d1081c759160774d9c0298c2e4504bd852c5a7a22e135079` |

The [0.2.9 release page](https://github.com/the-boring-crocodile/boring-pondnt/releases/tag/v0.2.9)
is the public release location; this documentation round did not verify its
current attachments. 0.2.10 (Pond Common 0.7.13, the bundled service versions in the CHANGELOG) is the
last candidate with only version numbers changed, its own and the bundled
services' (and MCP Ferry now reporting its own). The published 0.2.10 zip is
a new build; its hashes are on its release page, not here. See
[CHANGELOG](../CHANGELOG.md) and the
[browser first check](D365WEB.md#from-nothing-to-the-first-check-in-one-go)
for the recorded live workflow's scope.

## Per surface

| Surface | Status |
|---|---|
| Core tools (files, exec, jobs, batch) | native suite; live on both 10.0.49 dev VMs |
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
