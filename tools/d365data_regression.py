"""d365data regression run for a dev box (D48: the d365data user is an administrator there).

Runs through the gateway exactly as a Copilot client does, using the person's .mcp.json
(default %USERPROFILE%\\.mcp.json, entry "boring-gateway"). Run it with the installed kit's
python after every install:

    <kit>\\python\\python.exe d365data_regression.py [--company usmf] [--mcp-json PATH] [--out FILE]

Test data: everything it creates starts with ZZREG and is deleted again, except the fixture
batch job "ZZREG d365data regression (no tasks)", which must exist once (create it in the
Batch jobs form as a job without tasks; BatchJobs is read-only in OData). Without it the batch
check is skipped. Exit 0: no failures (skips allowed); 1: a check failed; 2: could not start.
Keys, tokens and storage URLs are never printed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

TOOLS = {"odata_entities", "odata_query", "odata_write", "odata_action", "dmf_export", "dmf_import", "dmf_status"}
FIXTURE_JOB = "ZZREG d365data regression (no tasks)"
TERMINAL_DMF = {"Succeeded", "PartiallySucceeded", "Failed", "Canceled"}


class Run:
    def __init__(self, session, company: str):
        self.s, self.company, self.results, self.t0 = session, company, [], time.monotonic()

    async def call(self, tool: str, args: dict, service: str = "d365data"):
        """(ok, data): ok False for refusals and failures; data is parsed JSON when possible."""
        if tool in ("service_describe",):
            r = await self.s.call_tool(tool, {"service": service})
        else:
            r = await self.s.call_tool("service_call", {"service": service, "tool": tool, "arguments": args})
        text = "".join(getattr(c, "text", "") for c in r.content)
        try:
            data = json.loads(text)
        except ValueError:
            data = text
        if isinstance(data, dict) and ("failure" in data or data.get("status") in ("refused", "failed")):
            return False, data
        return (not r.is_error), data

    def record(self, name: str, outcome: str, detail="") -> None:
        detail = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=True, default=str)
        self.results.append({"check": name, "outcome": outcome, "detail": detail[:600],
                             "t": round(time.monotonic() - self.t0, 1)})
        print(f"{outcome:4} {name}: {detail[:300]}", flush=True)

    def check(self, name: str, condition: bool, detail="") -> bool:
        self.record(name, "PASS" if condition else "FAIL", detail)
        return condition

    async def query(self, **args):
        ok, data = await self.call("odata_query", args)
        if not ok:
            raise RuntimeError(f"odata_query failed: {str(data)[:400]}")
        return data


async def describe(run: Run) -> None:
    ok, data = await run.call("service_describe", {})
    tools = {t["name"] for t in data.get("tools", [])} if ok and isinstance(data, dict) else set()
    run.check("describe.tools", tools == TOOLS, sorted(tools))
    run.check("describe.guide", ok and len((data or {}).get("guide") or "") > 500, f"guide chars {len((data or {}).get('guide') or '')}")


async def reads(run: Run) -> None:
    ok, data = await run.call("odata_entities", {"search": "CustomersV3"})
    run.check("entities.search", ok and "CustomersV3" in data.get("rows", []), data if not ok else data.get("rows"))
    ok, data = await run.call("odata_entities", {"entity": "CustomersV3"})
    run.check("entities.detail", ok and data.get("company_specific") is True and data.get("keys"), {"keys": data.get("keys") if ok else data})

    rows = (await run.query(entity="CustomersV3", select="CustomerAccount,dataAreaId", top=5))["rows"]
    default = {r["dataAreaId"] for r in rows}
    run.check("query.default_company", len(default) == 1, sorted(default))

    first = (await run.query(entity="CustomersV3", select="dataAreaId", cross_company=True, order_by="dataAreaId asc", top=1))["rows"]
    last = (await run.query(entity="CustomersV3", select="dataAreaId", cross_company=True, order_by="dataAreaId desc", top=1))["rows"]
    lo, hi = first[0]["dataAreaId"], last[0]["dataAreaId"]
    run.check("query.cross_company_all", lo.lower() != hi.lower(), f"first {lo}, last {hi}")
    other = hi if hi.lower() != run.company.lower() else lo
    rows = (await run.query(entity="CustomersV3", select="dataAreaId", company=other, top=20))["rows"]
    run.check("query.explicit_company", rows and all(r["dataAreaId"].lower() == other.lower() for r in rows), f"{other}: {len(rows)} rows")

    every = await run.query(entity="CustomersV3", select="CustomerAccount", cross_company=True, top=1, count=True)
    own = await run.query(entity="CustomersV3", select="CustomerAccount", top=1, count=True)
    run.check("query.count", isinstance(every.get("count"), int) and isinstance(own.get("count"), int)
              and 0 < own["count"] < every["count"], {"all_companies": every.get("count"), run.company: own.get("count")})
    page1 = await run.query(entity="CustomersV3", select="CustomerAccount", order_by="CustomerAccount", top=2)
    page2 = await run.query(next=page1["next"]) if page1.get("next") else {"rows": []}
    a = [r["CustomerAccount"] for r in page1["rows"]]; b = [r["CustomerAccount"] for r in page2["rows"]]
    run.check("query.paging", len(a) == 2 and len(b) == 2 and not set(a) & set(b), {"page1": a, "page2": b})

    cut = await run.query(entity="ReleasedProductsV2", top=200, max_chars=40000)
    run.check("query.cut_note", cut.get("truncated") is True and cut.get("shown", 0) > 0 and bool(cut.get("note")), {"shown": cut.get("shown"), "note": cut.get("note")})

    t = time.monotonic()
    big = await run.query(entity="CustomersV3", select="CustomerAccount,dataAreaId", cross_company=True, top=500, max_chars=262144)
    secs = time.monotonic() - t
    run.record("query.volume", "INFO", f"{big['shown']} rows in {secs:.1f} s ({big['shown'] / secs:.0f} rows/s)")


async def refusals(run: Run) -> None:
    cases = [
        ("refuse.top_0", "odata_query", {"entity": "CustomersV3", "top": 0}),
        ("refuse.top_501", "odata_query", {"entity": "CustomersV3", "top": 501}),
        ("refuse.unbalanced_filter", "odata_query", {"entity": "CustomersV3", "filter": "(CustomerAccount eq 'X'"}),
        ("refuse.foreign_next", "odata_query", {"next": "https://example.com/data/CustomersV3?$skip=2"}),
        ("refuse.max_chars", "odata_query", {"entity": "CustomersV3", "max_chars": 10_000_000}),
        ("refuse.slash_key", "odata_write", {"entity": "DocumentTypes", "op": "update", "key": {"ID": "ZZ/REG"},
                                             "values": {"Name": "x"}, "company": run.company}),
        ("refuse.dmf_via_action", "odata_action", {"entity": "DataManagementDefinitionGroups", "action": "ExportToPackage", "arguments": {}}),
    ]
    for name, tool, args in cases:
        ok, data = await run.call(tool, args)
        run.check(name, not ok, data)


async def write_cycle(run: Run) -> None:
    key, entity = {"ID": "ZZREG001"}, "DocumentTypes"
    await run.call("odata_write", {"entity": entity, "op": "delete", "key": key, "company": run.company})  # leftover from an aborted run
    template = (await run.query(entity=entity, filter="ID eq 'File'", company=run.company, top=1))["rows"]
    if not template:
        run.record("write.cycle", "SKIP", "no template row 'File' in DocumentTypes")
        return
    values = {k: v for k, v in template[0].items() if k not in ("dataAreaId", "ID", "Parameters") and not k.startswith("@")}
    values.update(ID="ZZREG001", Name="ZZREG regression")
    ok, data = await run.call("odata_write", {"entity": entity, "op": "create", "values": values, "company": run.company})
    if not run.check("write.create", ok and data.get("record", {}).get("ID") == "ZZREG001", data if not ok else "created"):
        return
    ok, data = await run.call("odata_write", {"entity": entity, "op": "update", "key": key, "values": {"Name": "ZZREG updated"}, "company": run.company})
    rows = (await run.query(entity=entity, filter="ID eq 'ZZREG001'", select="ID,Name", company=run.company))["rows"]
    run.check("write.update", ok and rows and rows[0]["Name"] == "ZZREG updated", rows or data)
    ok, data = await run.call("odata_write", {"entity": entity, "op": "delete", "key": key, "company": run.company})
    rows = (await run.query(entity=entity, filter="ID eq 'ZZREG001'", select="ID", company=run.company))["rows"]
    run.check("write.delete", ok and not rows, data if not ok else f"{len(rows)} rows left")


async def key_types(run: Run) -> None:
    """Enum key (NameAffixes: create/update/delete), decimal key with a non-ASCII string key (AddressFormatLines:
    no-op update of an existing row), unknown enum member refused locally, D365 text returned unmasked (D49)."""
    entity, key = "NameAffixes", {"Type": "PersonalPrefix", "Affix": "ZZREG"}
    await run.call("odata_write", {"entity": entity, "op": "delete", "key": key})  # leftover from an aborted run
    text = "Secret Santa: password reset, token ring"
    ok, data = await run.call("odata_write", {"entity": entity, "op": "create", "values": {**key, "Description": text}})
    if not run.check("keys.enum_create", ok, data if not ok else "created"):
        return
    rows = (await run.query(entity=entity, filter="Affix eq 'ZZREG'", select="Type,Affix,Description"))["rows"]
    run.check("keys.unmasked_text", rows and rows[0]["Description"] == text, rows)
    ok, data = await run.call("odata_write", {"entity": entity, "op": "update", "key": key, "values": {"Description": "ZZREG updated"}})
    rows = (await run.query(entity=entity, filter="Affix eq 'ZZREG'", select="Description"))["rows"]
    run.check("keys.enum_update", ok and rows and rows[0]["Description"] == "ZZREG updated", rows or data)
    ok, data = await run.call("odata_write", {"entity": entity, "op": "update", "key": {"Type": "NoSuchMember", "Affix": "ZZREG"}, "values": {"Description": "x"}})
    run.check("keys.enum_unknown_member_refused", not ok and isinstance(data, dict) and data.get("status") == "refused", data)
    ok, data = await run.call("odata_write", {"entity": entity, "op": "delete", "key": key})
    rows = (await run.query(entity=entity, filter="Affix eq 'ZZREG'", select="Affix"))["rows"]
    run.check("keys.enum_delete", ok and not rows, data if not ok else f"{len(rows)} rows left")
    lines = (await run.query(entity="AddressFormatLines", filter="LineNumber eq -1", select="AddressFormat,LineNumber,Separator", top=50))["rows"]
    row = next((r for r in lines if not r["AddressFormat"].isascii()), lines[0] if lines else None)
    if not row:
        run.record("keys.decimal_update", "SKIP", "no AddressFormatLines row with LineNumber -1")
        return
    ok, data = await run.call("odata_write", {"entity": "AddressFormatLines", "op": "update",
                                              "key": {"AddressFormat": row["AddressFormat"], "LineNumber": row["LineNumber"]},
                                              "values": {"Separator": row["Separator"]}})
    run.check("keys.decimal_update", ok, data if not ok else f"no-op update of {row['AddressFormat']!r}/{row['LineNumber']}")


async def wait_dmf(run: Run, execution_id: str, limit: float, download: bool = False):
    end, data = time.monotonic() + limit, {}
    while time.monotonic() < end:
        ok, data = await run.call("dmf_status", {"execution_id": execution_id})
        if not ok:
            return ok, data
        if data.get("status") in TERMINAL_DMF:
            break
        await asyncio.sleep(5)
    if download and data.get("status") == "Succeeded":
        return await run.call("dmf_status", {"execution_id": execution_id, "download": True})
    return True, data


async def dmf_round_trip(run: Run) -> None:
    export, imp = "ZZREG-EXPORT", "ZZREG-IMPORT"
    for name in (export, imp):
        await run.call("odata_write", {"entity": "DataManagementDefinitionGroups", "op": "delete", "key": {"Name": name}})
    try:
        ok, data = await run.call("odata_write", {"entity": "DataManagementDefinitionGroups", "op": "create", "values": {
            "Name": export, "Description": "d365data regression export", "OperationType": "Export", "GenerateDataPackage": "Yes"}})
        if not run.check("dmf.project_create", ok, data if not ok else export):
            return
        ok, data = await run.call("odata_write", {"entity": "DataManagementDefinitionGroupDetails", "op": "create", "values": {
            "DefinitionGroupId": export, "EntityName": "Document types", "SourceFormat": "CSV", "ExecutionUnit": 1,
            "LevelInExecutionUnit": 1, "SequenceInLevel": 1, "AutoGenerateMapping": "Yes", "DefaultRefreshType": "FullPush"}})
        if not run.check("dmf.entity_line", ok, data if not ok else "Document types"):
            return
        ok, data = await run.call("dmf_export", {"definition_group": export, "company": run.company, "package_name": "zzreg"})
        if not run.check("dmf.export_submit", ok and data.get("execution_id"), data):
            return
        ok, data = await wait_dmf(run, data["execution_id"], 300, download=True)
        package = data.get("package_path") if ok else None
        if not run.check("dmf.export_done", ok and data.get("status") == "Succeeded" and data.get("downloaded") and data.get("package_bytes", 0) > 0,
                         {k: data.get(k) for k in ("status", "downloaded", "package_bytes", "errors_shown")} if isinstance(data, dict) else data):
            return
        ok, data = await run.call("odata_write", {"entity": "DataManagementDefinitionGroups", "op": "create", "values": {
            "Name": imp, "Description": "d365data regression import", "OperationType": "Import"}})
        if not run.check("dmf.import_project", ok, data if not ok else imp):
            return
        ok, data = await run.call("dmf_import", {"definition_group": imp, "package_path": package, "company": run.company})
        if not run.check("dmf.import_submit", ok and data.get("execution_id"), data):
            return
        ok, data = await wait_dmf(run, data["execution_id"], 420)
        run.check("dmf.import_done", ok and data.get("status") == "Succeeded" and not data.get("errors"),
                  {k: data.get(k) for k in ("status", "errors_shown")} if isinstance(data, dict) else data)
    finally:
        for name in (export, imp):
            ok, data = await run.call("odata_write", {"entity": "DataManagementDefinitionGroups", "op": "delete", "key": {"Name": name}})
        left = (await run.query(entity="DataManagementDefinitionGroups", filter=f"Name eq '{export}' or Name eq '{imp}'", select="Name"))["rows"]
        run.check("dmf.cleanup", not left, f"{len(left)} projects left")


async def batch(run: Run) -> None:
    rows = (await run.query(entity="BatchJobs", filter=f"JobDescription eq '{FIXTURE_JOB}'", select="BatchJobRecId,Status"))["rows"]
    if len(rows) != 1:
        run.record("batch.restart", "SKIP", f"{len(rows)} fixture jobs named '{FIXTURE_JOB}' (need exactly one, without tasks)")
        return
    job = rows[0]["BatchJobRecId"]
    ok, data = await run.call("odata_action", {"entity": "BatchJobs", "action": "SetBatchJobToWaiting", "arguments": {"batchJobId": job}})
    value = data.get("value", {}) if ok and isinstance(data, dict) else {}
    if not run.check("batch.restart", value.get("IsSuccess") is True, value or data):
        return
    seen, end = [], time.monotonic() + 180
    while time.monotonic() < end:
        status = (await run.query(entity="BatchJobs", filter=f"BatchJobRecId eq {job}", select="Status"))["rows"][0]["Status"]
        if not seen or seen[-1] != status:
            seen.append(status)
        if status in ("Finished", "Error", "Canceled", "Ended"):
            break
        await asyncio.sleep(5)
    run.check("batch.finished", seen and seen[-1] == "Finished", seen)
    ok, data = await run.call("odata_action", {"entity": "BatchJobs", "action": "SetBatchJobToWaiting", "arguments": {"batchJobId": 1}})
    run.check("batch.bad_id", ok and data.get("value", {}).get("IsSuccess") is False, data)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--company", default="usmf")
    parser.add_argument("--mcp-json", default=os.path.expandvars(r"%USERPROFILE%\.mcp.json"))
    parser.add_argument("--entry", default="boring-gateway")
    parser.add_argument("--out")
    parser.add_argument("--skip", default="", help="comma list of parts: reads,refusals,write,keys,dmf,batch")
    a = parser.parse_args()
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    try:
        entry = json.load(open(a.mcp_json, encoding="utf-8"))["servers"][a.entry]
    except (OSError, ValueError, KeyError) as e:
        print(f"cannot read {a.entry} from {a.mcp_json}: {type(e).__name__}", file=sys.stderr)
        return 2
    skip = {s for s in a.skip.split(",") if s}
    async with httpx2.AsyncClient(headers=entry.get("headers", {}), timeout=180) as http, \
            streamable_http_client(entry["url"], http_client=http) as stream:
        async with ClientSession(stream[0], stream[1]) as session:
            await session.initialize()
            run = Run(session, a.company)
            parts = [("describe", describe), ("reads", reads), ("refusals", refusals), ("write", write_cycle), ("keys", key_types),
                     ("dmf", dmf_round_trip), ("batch", batch)]
            for name, part in parts:
                if name in skip:
                    run.record(name, "SKIP", "skipped by --skip")
                    continue
                try:
                    await part(run)
                except Exception as e:  # keep going; a crash in one part is a failure of that part
                    run.record(name, "FAIL", f"{type(e).__name__}: {str(e)[:400]}")
    counts = {k: sum(r["outcome"] == k for r in run.results) for k in ("PASS", "FAIL", "SKIP", "INFO")}
    print("summary:", json.dumps(counts), f"{round(time.monotonic() - run.t0)} s")
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump({"counts": counts, "results": run.results}, f, indent=1)
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    # D365 text can be non-Latin ("Россия"); a cp1252 console or a redirected stream
    # would stop the run with UnicodeEncodeError while printing (a dev box, 2026-11-04).
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(asyncio.run(main()))
