# Changelog

Notable changes per release, newest first ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
[SemVer](https://semver.org/); tags `vX.Y.Z`, candidates `vX.Y.Z-rc.N`).

## 0.2.0rc23 (tag v0.2.0-rc.23) — 2026-10-22

- Kit (`visualstudio.md`): edit your own X++ elements through the solution's
  source-repository path, not the `PackagesLocalDirectory` path a search may
  return; on FileNotFound from an edit tool, retry with the solution-relative
  path or `edit_file`. Observed on a D365 box with Visual Studio 2022.
- INSTALL step 1 starts at the download: where releases are, `curl.exe`, the
  hash check, and why `Invoke-WebRequest` can seem to hang.
- The bundle no longer carries pywin32's Pythonwin and Windows-service host,
  which pondNT never runs.

## 0.2.0rc22 (tag v0.2.0-rc.22) — 2026-10-21

First public release of pondNT, MCP servers for Visual Studio Copilot on a Windows Dynamics 365 development machine, with its working agreement for Copilot.
