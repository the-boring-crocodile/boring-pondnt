# What has been tested where

pondNT is built on Linux and runs on Windows next to D365, so "the tests
pass" can mean very different things. This page says which kind of
evidence exists for the current release, and what has not been tested at
all. A row that is not here is untested.

## Kinds of evidence

| Kind | What it shows | What it does not |
|---|---|---|
| **Portable CI** (GitHub Actions, Ubuntu 24.04, Python 3.10, 3.12, 3.14) | the code and its tests on Linux; Windows-only tests skip themselves | anything about Windows or D365 |
| **Native suite** (the release zip's `run-tests.cmd` and `run-services-tests.cmd` on a Windows D365 box) | processes, encodings, locks, the installer journey and the bundled services on real Windows | live D365 behaviour: the D365 tests there use recorded fixtures |
| **Live on a dev box** | the tools against a running D365: builds, xref, SQL, browser forms, warehouse flows | another D365 version, topology or customisation |
| **Reported use** | someone used a release for real work and reports no problem | what exactly was exercised |

## Where

| Environment | Windows | D365 | Evidence for 0.2.8 and 0.2.9 |
|---|---|---|---|
| Local Hyper-V dev VM (VHD), our test box | Windows Server 2022 Datacenter, build 20348; Windows PowerShell 5.1; SQL Server 2019 (15.0.4480); Visual Studio 2022 17.14 | application 10.0.49, platform Update 73 (7.0.8199.32), Contoso demo data | native suite: 696 tests, 0 failures; bundled services: 515, 0 failures; installer journey; live: xref, SQL read-back, browser forms in visible, covered and headless Edge, gateway routing ([first check](D365WEB.md#from-nothing-to-the-first-check-in-one-go)) |
| Azure cloud-hosted dev VM, used for client work | Windows Server (version not recorded here) | not recorded here | reported use of 0.2.7 through Copilot in Visual Studio for development and testing; 0.2.8 installed, its first check pending; 0.2.9 not installed |

Shared by both: a release zip of the same kind (bundled Python 3.14 and
PowerShell 7), Copilot in Visual Studio as the client. The counts above are
from the native run of the published 0.2.9 build: its summary (commit, both
zips' SHA-256, counts per suite) is attached to the release on GitHub.

## Per surface

| Surface | Status |
|---|---|
| Core tools (files, exec, jobs, batch) | native suite; live on the test box |
| d365: build, results, sync, best practice, `d365_run` | native suite with fixtures; build, results and AOS restarts live on the test box; syncs run live there during development |
| d365: `xref` | live on the test box (DYNAMICSXREFDB, 5.3 million names) |
| d365: `d365_sql` | live on the test box |
| d365web (browser forms) | native suite (80 tests); live on standard Contoso forms on the test box, including an inventory movement journal |
| d365whsapp, `form` mode (in-browser simulator) | native suite; a receiving suite of 12 cases (purchase, transfer and ASN receipts, each good and bad) passed in form mode on a development box, as reported to us |
| d365whsapp, `app` mode (the mobile app protocol) | native suite with fixtures only; **not accepted against a live environment** |
| The physical device or Microsoft's mobile app | **not tested**; neither mode exercises them |
| Unified developer experience (UDE) | **not tested** |
| Windows 10/11 client machines | **not tested**; the targets are Windows Server dev boxes |
| Visual Studio 2022 17.14 (Copilot, Agent mode) | the tested client |
| VS Code, Claude Code, Claude Desktop | **untested recipes** ([CLIENTS.md](CLIENTS.md)) |

## Efficiency

Whether the specialist tools make an agent cheaper has not been measured
properly: that needs a real scenario, repeated runs and a fair comparison.
