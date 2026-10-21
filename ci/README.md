# ci

`guest-run.ps1` runs a test release in a disposable Windows guest: build it
with `scripts/build.py --with-tests`, copy the zip and this script into the
guest, then

    powershell -NoProfile -ExecutionPolicy Bypass -File C:\ci\guest-run.ps1 -Zip C:\ci\boring-pondnt-VERSION-win-x64-tests.zip

It enables long paths if needed (and says so), unpacks the release into
`C:\boring-pondnt-ci`, runs `boring-pondnt-core.cmd --self-check`, the whole
test suite (`run-tests.cmd`) and the bundled service tests
(`run-services-tests.cmd`), and writes `C:\boring-pondnt-ci\results.json`
(`tests`, `services_tests`); exit code 0 only if all three passed.
