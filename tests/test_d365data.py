"""OData data service using synthetic metadata and a fake HTTP transport."""
import asyncio
import http.client
import json
import urllib.error
import urllib.parse

import pytest

from mcp.server.mcpserver.exceptions import ToolError
from boring_service_kit.outcomes import Failed, Refused
from boring_pondnt.d365_entra import Response
from boring_pondnt.d365data_client import Config, DataClient
from boring_pondnt.d365data_server import create_server

METADATA = """<?xml version="1.0"?>
<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">
<edmx:DataServices><Schema xmlns="http://docs.oasis-open.org/odata/ns/edm" Namespace="Microsoft.Dynamics.DataEntities">
<EntityType Name="Customer"><Key><PropertyRef Name="dataAreaId"/><PropertyRef Name="Account"/></Key>
<Property Name="dataAreaId" Type="Edm.String" Nullable="false"/>
<Property Name="Account" Type="Edm.String" Nullable="false"/><Property Name="Name" Type="Edm.String"/></EntityType>
<EntityType Name="Batch"><Key><PropertyRef Name="BatchJobId"/></Key><Property Name="BatchJobId" Type="Edm.Int64"/></EntityType>
<EntityContainer Name="Container"><EntitySet Name="Customers" EntityType="Microsoft.Dynamics.DataEntities.Customer"/>
<EntitySet Name="BatchJobs" EntityType="Microsoft.Dynamics.DataEntities.Batch"/></EntityContainer>
</Schema></edmx:DataServices></edmx:Edmx>"""


class Fake:
    metadata = METADATA

    def __init__(self):
        self.calls = []
        self.query_reply = Response(200, json.dumps({"value": [{"Account": "A1"}],
                                                    "@odata.nextLink": "https://aos.example/data/Customers?$skip=1"}))
    def __call__(self, url, headers, body, timeout, **kwargs):
        self.calls.append((url, headers, body, timeout, kwargs))
        if "login.microsoftonline.com" in url:
            return Response(200, json.dumps({"access_token": "token", "expires_in": 3600}))
        assert headers["Authorization"] == "Bearer token"
        if url.endswith("/data/$metadata"):
            return Response(200, self.metadata)
        return self.query_reply


def make(fake, **kwargs):
    return DataClient(Config("https://aos.example", "tenant", "client", "secret.dpapi", **kwargs),
                      transport=fake, secret=lambda: "secret")


def invoke(server, name, args):
    result = asyncio.run(server.call_tool(name, args))
    blocks = result[0] if isinstance(result, tuple) else result
    content = blocks.content if hasattr(blocks, "content") else blocks
    return getattr(blocks, "is_error", False), json.loads(content[0].text)


def test_entities_and_metadata_cache():
    fake = Fake()
    client = make(fake)
    assert client.entities(search="jobs")["rows"] == ["BatchJobs"]
    details = client.entities(entity="Customers")
    assert details["keys"] == ["dataAreaId", "Account"]
    assert details["company_specific"] is True
    assert details["fields"][0] == {"name": "dataAreaId", "type": "Edm.String", "nullable": False}
    assert client.entities(entity="BatchJobs")["company_specific"] is False
    metadata_calls = [c for c in fake.calls if c[0].endswith("/data/$metadata")]
    assert len(metadata_calls) == 1
    assert metadata_calls[0][1]["Accept"] == "application/xml"
    assert metadata_calls[0][4]["max_bytes"] == 128 * 1024 * 1024


def test_query_company_filter_is_escaped_and_cross_company():
    fake = Fake()
    client = make(fake)
    result = client.query("Customers", filter="Name eq 'X'", select="Account,Name",
                          order_by="Account desc", company="o'brien", top=2)
    assert result["rows"] == [{"Account": "A1"}] and result["next"].endswith("$skip=1")
    assert not result["truncated"]
    url, _, _, _, options = fake.calls[-1]
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert params == {"$filter": ["(Name eq 'X') and dataAreaId eq 'o''brien'"],
                      "$select": ["Account,Name"], "$orderby": ["Account desc"], "cross-company": ["true"]}
    assert fake.calls[-1][1]["Prefer"] == "odata.maxpagesize=2"
    assert options["method"] == "GET"


@pytest.mark.parametrize(("filter", "company", "config"), [
    ("a eq 1) or (b eq 2", "usmf", {}),
    ("startswith(Name,'x'", None, {"company": "usmf"}),
    ("Name eq 'x''", "usmf", {}),
    ("a eq 1) or (b eq 2", None, {}),
])
def test_unbalanced_query_filter_refused_before_token_or_request(filter, company, config):
    fake = Fake()
    with pytest.raises(Refused) as raised:
        make(fake, **config).query("Customers", filter=filter, company=company)
    assert raised.value.code == "invalid_request"
    assert fake.calls == []


@pytest.mark.parametrize("filter", ["Name eq 'x)'", "(a eq 1 or b eq 2)", "Name eq 'O''Brien)'"])
def test_balanced_query_filter_keeps_original_text(filter):
    fake = Fake()
    make(fake).query("Customers", filter=filter, company="usmf")
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.calls[-1][0]).query)
    assert params["$filter"] == [f"({filter}) and dataAreaId eq 'usmf'"]


def test_bounds_and_errors():
    fake = Fake()
    client = make(fake)
    with pytest.raises(Refused):
        client.query("Customers", top=501)
    with pytest.raises(Refused):
        client.query("../Customers")
    with pytest.raises(Refused):
        client.query("BatchJobs", company="usmf")
    fake.query_reply = Response(400, '{"error":{"message":"invalid filter"}}')
    with pytest.raises(Failed) as raised:
        client.query("Customers")
    assert raised.value.code == "d365_error" and "invalid filter" in raised.value.message
    fake.query_reply = Response(200, "x", truncated=True)
    with pytest.raises(Failed) as raised:
        client.query("Customers")
    assert raised.value.code == "limit" and raised.value.details["truncated"] is True
    fake.query_reply = Response(200, '{"value":"bad"}')
    with pytest.raises(Failed) as raised:
        client.query("Customers")
    assert raised.value.code == "bad_reply"


@pytest.mark.parametrize("next_page", [None, "https://aos.example/data/Customers?$skip=3"])
def test_query_byte_cut_reports_skipped_rows_when_next_is_present(next_page):
    fake = Fake()
    rows = [{"text": "a" * 16_384}] * 3
    reply = {"value": rows}
    if next_page:
        reply["@odata.nextLink"] = next_page
    fake.query_reply = Response(200, json.dumps(reply))
    result = make(fake).query("Customers", top=3)
    assert result["truncated"] and result["shown"] == 1 and result["rows"] == rows[:1]
    assert result["next"] == next_page
    if next_page:
        assert result["note"] == (
            "Only the first 1 of 3 rows on this page fit; the next link continues after the whole page. "
            "Repeat with a smaller top, a narrower select, or a larger max_chars to get the rest."
        )
    else:
        assert result["note"] == (
            "Only the first 1 of 3 rows fit and D365 sent no next link; repeat with "
            "top=1 (or smaller), a narrower select, or a larger max_chars, and page with next."
        )


@pytest.mark.parametrize("continuation", [False, True])
def test_wide_query_page_uses_32_mib_transport_cap(continuation):
    class CappedFake(Fake):
        def __call__(self, url, headers, body, timeout, **kwargs):
            reply = super().__call__(url, headers, body, timeout, **kwargs)
            if reply is self.query_reply and len(reply.text) > kwargs["max_bytes"]:
                return Response(200, "", truncated=True)
            return reply

    fake = CappedFake()
    link = "https://aos.example/data/Customers?$skip=500"
    rows = [{"Name": "x" * 6_291}] * 500
    body = json.dumps({"value": rows, "@odata.nextLink": link})
    assert 3 * 1024 * 1024 < len(body) < 4 * 1024 * 1024
    fake.query_reply = Response(200, body)
    client = make(fake)
    args = {"next": link} if continuation else {"entity": "Customers", "top": 500}
    result = client.query(**args)
    assert fake.calls[-1][4]["max_bytes"] == 32 * 1024 * 1024
    assert result["rows"] == rows[:5] and result["truncated"]
    assert result["next"] == link and "first 5 of 500 rows" in result["note"]

    fake.query_reply = Response(200, "", truncated=True)
    with pytest.raises(Failed) as raised:
        client.query(**args)
    assert raised.value.code == "limit" and raised.value.details["truncated"]
    assert raised.value.message == (
        "D365's page was larger than 32 MiB; repeat with a smaller top or a narrower select"
    )


def test_no_next_and_zero_rows_does_not_suggest_top_zero():
    fake = Fake()
    fake.query_reply = Response(200, '{"value":[{"Name":"wide"}]}')
    result = make(fake).query("Customers", max_chars=1)
    assert result["truncated"] and result["shown"] == 0 and not result["next"]
    assert "first 0 of 1 rows" in result["note"]
    assert "narrower select" in result["note"] and "larger max_chars" in result["note"]
    assert "top=0" not in result["note"]


@pytest.mark.parametrize("args, field, advice", [
    ({"search": "s"}, "rows", "narrow the search"),
    ({"entity": "Customers"}, "fields", "raise max_chars"),
])
def test_entity_cut_explains_recovery(tmp_path, args, field, advice):
    fake = Fake()
    server = create_server(make(fake), tmp_path)
    error, result = invoke(server, "odata_entities", {**args, "max_chars": 1})
    assert not error and result["truncated"] and result[field] == []
    assert advice in result["note"] and "Only the first 0 of " in result["note"]


@pytest.mark.parametrize("tool, args, field", [
    ("odata_query", {"entity": "Customers"}, "top"),
    ("odata_query", {"entity": "Customers"}, "max_chars"),
    ("odata_entities", {}, "max_chars"),
    ("odata_write", {"entity": "Customers", "op": "create", "values": {"Account": "A1"}}, "max_chars"),
    ("odata_action", {"entity": "BatchJobs", "action": "SetBatchJobToWaiting"}, "max_chars"),
    ("dmf_status", {"execution_id": "id"}, "max_chars"),
])
def test_boolean_integer_options_refused_before_token_or_http(tmp_path, tool, args, field):
    fake = Fake()
    server = create_server(make(fake), tmp_path)
    with pytest.raises(ToolError, match=field):
        invoke(server, tool, {**args, field: True})
    assert not fake.calls


def test_query_row_limit_reports_skipped_rows_when_next_is_present():
    fake = Fake()
    link = "https://aos.example/data/Customers?$skip=2"
    fake.query_reply = Response(200, json.dumps({"value": [{"Account": "A1"}, {"Account": "A2"}],
                                                "@odata.nextLink": link}))
    result = make(fake).query("Customers", top=1)
    assert result["rows"] == [{"Account": "A1"}] and result["shown"] == 1
    assert result["truncated"] and result["next"] == link
    assert "first 1 of 2 rows" in result["note"] and "after the whole page" in result["note"]
    assert "smaller top" in result["note"] and "narrower select" in result["note"]
    assert "max_chars" in result["note"]


@pytest.mark.parametrize("status, error, expected", [
    (403, {"code": "", "message": "An error has occurred.",
           "innererror": {"message": "User is not authorized to read this entity.",
                          "type": "Microsoft.Dynamics.Ax.Xpp.ErrorException",
                          "stacktrace": "   at Microsoft.Dynamics.Platform.Integration"}},
     {"code": "", "message": "An error has occurred.",
      "innererror": {"message": "User is not authorized to read this entity.",
                     "type": "Microsoft.Dynamics.Ax.Xpp.ErrorException"}}),
    (400, {"code": "BadRequest", "message": "An error has occurred.", "stacktrace": "   at Top",
           "innererror": {"message": "Invalid field.", "type": "ODataException", "stacktrace": "   at Outer",
                          "internalexception": {"message": "Validation failed.", "type": "ValidationException",
                                                "stacktrace": "   at Middle",
                                                "internalexception": {"message": "Field is not queryable.",
                                                                      "type": "ArgumentException",
                                                                      "stacktrace": "   at Inner"}}}},
     {"code": "BadRequest", "message": "An error has occurred.",
      "innererror": {"message": "Invalid field.", "type": "ODataException",
                     "internalexception": {"message": "Validation failed.", "type": "ValidationException",
                                           "internalexception": {"message": "Field is not queryable.",
                                                                 "type": "ArgumentException"}}}}),
])
def test_odata_errors_keep_messages_without_stack_frames(status, error, expected, tmp_path):
    fake = Fake()
    fake.query_reply = Response(status, json.dumps({"error": error}))
    server = create_server(make(fake), tmp_path)
    is_error, outcome = invoke(server, "odata_query", {"entity": "Customers"})
    assert is_error and outcome["code"] == "d365_error"
    assert outcome["details"]["http_status"] == status
    prefix, body = outcome["message"].split(": ", 1)
    assert prefix == f"D365 returned HTTP {status}"
    assert json.loads(body) == expected
    assert "   at " not in body and "stacktrace" not in body


@pytest.mark.parametrize("continuation", [False, True])
def test_query_default_limit_and_raised_budget_through_tool(tmp_path, continuation):
    fake = Fake()
    rows = [{"Name": "a" * 16_384}, {"Name": "b" * 16_384}, {"Name": "c" * 16_384}]
    fake.query_reply = Response(200, json.dumps({"value": rows}))
    server = create_server(make(fake), tmp_path)
    args = {"next": "https://aos.example/data/Customers?$skip=3"} if continuation else {"entity": "Customers"}
    error, result = invoke(server, "odata_query", args)
    assert not error and result["rows"] == rows[:1] and result["truncated"]
    error, result = invoke(server, "odata_query", {**args, "max_chars": 262_144})
    assert not error and result["rows"] == rows and not result["truncated"]


@pytest.mark.parametrize("tool, args, field", [
    ("odata_entities", {}, "rows"),
    ("odata_entities", {"entity": "Customers"}, "fields"),
    ("odata_write", {"entity": "Customers", "op": "create", "values": {"Account": "A1"}}, "record"),
    ("odata_write", {"entity": "BatchJobs", "op": "delete", "key": {"BatchJobId": 123}}, "deleted"),
    ("odata_action", {"entity": "BatchJobs", "action": "SetBatchJobToWaiting"}, "value"),
])
def test_small_result_budget_is_forwarded_by_tools(tmp_path, tool, args, field):
    fake = Fake()
    fake.query_reply = Response(200, '{"value":"accepted"}')
    server = create_server(make(fake), tmp_path)
    error, result = invoke(server, tool, {**args, "max_chars": 1})
    assert not error and result["truncated"]
    assert result[field] == ([] if tool == "odata_entities" else None)


@pytest.mark.parametrize("max_chars", [0, 262_145])
@pytest.mark.parametrize("tool, args", [
    ("odata_query", {"entity": "Customers"}),
    ("odata_entities", {"refresh": True}),
    ("odata_write", {"entity": "Customers", "op": "create", "values": {"Account": "A1"}}),
    ("odata_action", {"entity": "BatchJobs", "action": "SetBatchJobToWaiting"}),
    ("dmf_status", {"execution_id": "id", "download": True}),
])
def test_bad_result_budget_refused_before_token_or_http(tmp_path, max_chars, tool, args):
    fake = Fake()
    server = create_server(make(fake), tmp_path)
    error, outcome = invoke(server, tool, {**args, "max_chars": max_chars})
    assert error and outcome["code"] == "invalid_request"
    assert "max_chars" in outcome["message"]
    assert not fake.calls


@pytest.mark.parametrize("tool, args, field", [
    ("odata_query", {"entity": "Customers"}, "rows"),
    ("odata_write", {"entity": "Customers", "op": "create", "values": {"Account": "A1"}}, "record"),
    ("odata_write", {"entity": "Customers", "op": "update", "company": "usmf",
                     "key": {"Account": "A1"}, "values": {"Name": "n"}}, "record"),
])
def test_etag_is_opt_in_through_tool(tmp_path, tool, args, field):
    fake = Fake()
    row = {"Account": "A1", "@odata.etag": 'W/"fixture"'}
    fake.query_reply = Response(200, json.dumps({"value": [row]} if field == "rows" else row))
    server = create_server(make(fake), tmp_path)
    for options, expected in [({}, {"Account": "A1"}), ({"include_etag": True}, row)]:
        error, result = invoke(server, tool, {**args, **options})
        assert not error and result[field] == ([expected] if field == "rows" else expected)


def test_unusable_token_reply_maps_to_d365data_failure(tmp_path):
    requests = []

    def transport(url, *args, **kwargs):
        requests.append(url)
        return Response(200, "not JSON")

    server = create_server(make(transport), tmp_path)
    error, outcome = invoke(server, "odata_query", {"entity": "Customers"})
    assert error and outcome["status"] == "failed" and outcome["code"] == "token_failed"
    assert outcome["message"] == "Entra returned an unusable token reply"
    assert requests == ["https://login.microsoftonline.com/tenant/oauth2/v2.0/token"]


@pytest.mark.parametrize("tool,args,field,wrap", [
    ("odata_query", {"entity": "Customers"}, "rows", lambda row: {"value": [row]}),
    ("odata_write", {"entity": "Customers", "op": "create", "values": {"Account": "A1"}}, "record", dict),
    ("odata_write", {"entity": "Customers", "op": "update", "company": "usmf",
                     "key": {"Account": "A1"}, "values": {"Name": "n"}}, "record", dict),
    ("odata_action", {"entity": "BatchJobs", "action": "SetBatchJobToWaiting"},
     "value", lambda row: {"value": row}),
])
def test_server_preserves_odata_values(tmp_path, tool, args, field, wrap):
    row = {"Description": "Secret Santa gift box", "PaymentToken": "abc",
           "Notes": "password reset done", "Extra": '{ "token" : "abc" , "x":  1 }'}
    fake = Fake()
    fake.query_reply = Response(200, json.dumps(wrap(row)))
    error, result = invoke(create_server(make(fake), tmp_path), tool, args)
    assert not error and result[field] == ([row] if field == "rows" else row)


def test_entra_http_failure_exposes_only_diagnostic_fields(tmp_path):
    response = {"error": "invalid_client", "error_codes": [7000215, 70002, False],
                "error_description": ("AADSTS7000215: invalid credential for "
                                      "0000aaaa-11bb-cccc-dd22-eeeeee333333 "
                                      "Bearer PRIVATE Trace ID: trace\r\nCorrelation ID: correlation"),
                "trace_id": "trace", "correlation_id": "correlation", "timestamp": "timestamp",
                "extra": "private raw body"}
    server = create_server(make(lambda *args, **kwargs: Response(401, json.dumps(response))), tmp_path)
    error, result = invoke(server, "odata_query", {"entity": "Customers"})
    assert error and result["code"] == "token_failed"
    assert result["details"] == {"error": "invalid_client", "error_codes": [7000215, 70002]}
    assert result["message"] == ("Entra token request: HTTP 401: invalid_client: "
                                 "AADSTS7000215: invalid credential for [ID omitted] Bearer ***")


def test_server_masks_credentials_in_odata_error(tmp_path):
    fake = Fake()
    message = ("Secret Santa failed https://storage.example/pkg?sig=private-url "
               "Bearer private-bearer Authorization: Basic private-basic sig=private-sig")
    fake.query_reply = Response(400, json.dumps({"error": {"message": message}}))
    error, result = invoke(create_server(make(fake), tmp_path), "odata_query", {"entity": "Customers"})
    assert error and result["code"] == "d365_error"
    prefix, body = result["message"].split(": ", 1)
    assert prefix == "D365 returned HTTP 400"
    assert json.loads(body)["message"] == (
        "Secret Santa failed [URL omitted] Bearer *** Authorization: *** sig=***")


def test_write_create_update_delete_and_key_escaping():
    fake = Fake()
    client = make(fake)
    fake.query_reply = Response(201, '{"Account":"new"}')
    assert client.write("Customers", "create", values={"Account": "new"}, company="usmf")["record"]["Account"] == "new"
    assert fake.calls[-1][4]["method"] == "POST"
    assert json.loads(fake.calls[-1][2]) == {"Account": "new", "dataAreaId": "usmf"}
    fake.query_reply = Response(200, '{"Account":"A"}')
    account = "O'Brien-ä😀"
    client.write("Customers", "update", key={"Account": account}, values={"Name": "New"}, company="usmf")
    url, _, _, _, opts = fake.calls[-1]
    assert opts["method"] == "PATCH"
    assert "Account='O''Brien-%C3%A4%F0%9F%98%80'" in url
    assert "dataAreaId='usmf'" in url and url.endswith("?cross-company=true")
    fake.query_reply = Response(204, "")
    result = client.write("Customers", "delete", key={"Account": "A"}, company="usmf")
    assert result == {"deleted": {"Account": "A", "dataAreaId": "usmf"}, "truncated": False}
    assert fake.calls[-1][4]["method"] == "DELETE"


@pytest.mark.parametrize("account", ["A/1", r"A\1"])
@pytest.mark.parametrize("op", ["update", "delete"])
def test_path_separator_in_key_refused_before_token_or_http(account, op):
    fake = Fake()
    client = make(fake)
    values = {"Name": "n"} if op == "update" else None
    with pytest.raises(Refused) as raised:
        client.write("Customers", op, key={"Account": account}, values=values, company="usmf")
    assert raised.value.code == "invalid_request"
    assert raised.value.message == (
        "D365 OData cannot address a key containing / or \\ in the URL path; "
        "change or delete the record in the D365 client"
    )
    assert not fake.calls


@pytest.mark.parametrize("account", ["A/1", r"A\1"])
def test_create_with_path_separator_in_key_succeeds_and_warns(account):
    fake = Fake()
    fake.query_reply = Response(204, "")
    result = make(fake).write("Customers", "create", values={"Account": account, "Name": "n"})
    assert json.loads(fake.calls[-1][2])["Account"] == account
    assert fake.calls[-1][4]["method"] == "POST" and result["record"] is None
    assert "no response body" in result["note"]
    assert "cannot be updated or deleted through OData" in result["note"]
    assert "Use the D365 client" in result["note"]


def test_create_warns_about_server_assigned_key_even_when_record_is_cut():
    fake = Fake()
    fake.query_reply = Response(201, '{"Account":"A/1","Name":"n"}')
    result = make(fake).write("Customers", "create", values={"Name": "n"}, max_chars=1)
    assert result["record"] is None and result["truncated"]
    assert "exceeded the result limit" in result["note"]
    assert "cannot be updated or deleted through OData" in result["note"]


@pytest.mark.parametrize("op", ["update", "delete"])
def test_write_accepts_company_case_difference(op):
    fake = Fake()
    fake.query_reply = Response(204, "")
    values = {"Name": "n", "dataAreaId": "USMF"} if op == "update" else None
    result = make(fake).write("Customers", op, key={"Account": "a", "dataAreaId": "USMF"},
                              company="usmf", values=values)
    assert "dataAreaId='usmf'" in fake.calls[-1][0]
    assert not result["truncated"]


@pytest.mark.parametrize("operation", ["query", "write"])
def test_explicit_empty_company_refused_before_transport(operation):
    fake = Fake()
    client = make(fake, company="usmf")
    with pytest.raises(Refused, match="omit it, or use cross_company") as error:
        if operation == "query":
            client.query("Customers", company="")
        else:
            client.write("Customers", "create", values={"Name": "n"}, company="")
    assert error.value.code == "invalid_request"
    assert not fake.calls


def test_create_with_path_separator_in_nonkey_value_does_not_warn():
    fake = Fake()
    fake.query_reply = Response(201, '{"Account":"A1","Name":"a/b"}')
    result = make(fake).write("Customers", "create", values={"Account": "A1", "Name": "a/b"})
    assert "note" not in result


@pytest.mark.parametrize(("edm_type", "value"), [
    ("Edm.Date", "2026-02-30"), ("Edm.Date", "20261104"),
    ("Edm.DateTimeOffset", "2026-11-04T00:00:00"),
    ("Edm.DateTimeOffset", "2026-11-04T00:00:00+01:60"),
    ("Edm.Decimal", "NaN"), ("Edm.Decimal", True),
    ("Microsoft.Dynamics.DataEntities.Mode", "Unknown"),
])
def test_invalid_key_literals_refused_before_write(edm_type, value):
    fake = Fake()
    fake.metadata = METADATA.replace("Edm.Int64", edm_type).replace(
        "<EntityContainer", '<EnumType Name="Mode"><Member Name="Active"/></EnumType><EntityContainer')
    client = make(fake)
    client.entities()
    before = len(fake.calls)
    with pytest.raises(Refused, match=edm_type) as error:
        client.write("BatchJobs", "delete", key={"BatchJobId": value})
    assert error.value.code == "invalid_request"
    assert len(fake.calls) == before


@pytest.mark.parametrize(("edm_type", "value", "literal"), [
    ("Edm.Guid", "01234567-89ab-cdef-0123-456789abcdef", "01234567-89ab-cdef-0123-456789abcdef"),
    ("Edm.Boolean", True, "true"),
    ("Edm.Boolean", False, "false"),
    ("Edm.Int32", -27, "-27"),
    ("Edm.Date", "2026-11-04", "2026-11-04"),
    ("Edm.DateTimeOffset", "2026-11-04T00:00:00Z", "2026-11-04T00%3A00%3A00Z"),
    ("Edm.DateTimeOffset", "2026-11-04T00:00:00.1234567+01:30", "2026-11-04T00%3A00%3A00.1234567%2B01%3A30"),
    ("Edm.Decimal", 1e-7, "0.0000001"),
    ("Edm.Decimal", "12345678901234567890.01", "12345678901234567890.01"),
    ("Microsoft.Dynamics.DataEntities.Mode", "Active", "Microsoft.Dynamics.DataEntities.Mode'Active'"),
    ("Model.Mode", "Active", "Microsoft.Dynamics.DataEntities.Mode'Active'"),
    ("Model.Unlisted", "Active", "Microsoft.Dynamics.DataEntities.Unlisted'Active'"),
])
def test_key_literal_is_sent_in_odata_path(edm_type, value, literal):
    fake = Fake()
    fake.metadata = METADATA.replace("Edm.Int64", edm_type).replace(
        'Namespace="Microsoft.Dynamics.DataEntities"', 'Namespace="Microsoft.Dynamics.DataEntities" Alias="Model"'
    ).replace("<EntityContainer", '<EnumType Name="Mode"><Member Name="Active"/></EnumType>'
              '<EnumType Name="Unlisted"/><EntityContainer')
    fake.query_reply = Response(204, "")
    result = make(fake).write("BatchJobs", "delete", key={"BatchJobId": value})
    assert result["deleted"] == {"BatchJobId": value}
    assert fake.calls[-1][0] == f"https://aos.example/data/BatchJobs(BatchJobId={literal})"
    assert fake.calls[-1][4]["method"] == "DELETE"


def test_unsupported_key_type_is_named():
    fake = Fake()
    fake.metadata = METADATA.replace("Edm.Int64", "Edm.Binary")
    client = make(fake)
    client.entities()
    before = len(fake.calls)
    with pytest.raises(Refused) as error:
        client.write("BatchJobs", "delete", key={"BatchJobId": "x"})
    assert error.value.code == "invalid_request"
    assert error.value.message == "unsupported or invalid key value for Edm.Binary"
    assert len(fake.calls) == before


@pytest.mark.parametrize(("op", "args", "message"), [
    ("update", {"key": {"Account": "x"}, "values": {"Name": "n"}}, "key must contain every entity key field"),
    ("delete", {"key": {"Account": "x", "dataAreaId": "x"}, "company": "y"}, "company conflicts with dataAreaId"),
    ("create", {"values": {"Name": {"nested": 1}}}, "nested values are not supported"),
    ("create", {"values": {"Unknown": "x"}}, "unknown entity field"),
    ("update", {"key": {"Account": "x", "dataAreaId": "x"}, "values": {}}, "create and update require values"),
])
def test_writes_refuse_bad_keys_company_and_deep_inserts(op, args, message):
    fake = Fake()
    client = make(fake)
    client.entities()  # Warm metadata so an invalid write makes no further call.
    before = len(fake.calls)
    with pytest.raises(Refused) as error:
        client.write("Customers", op, **args)
    assert error.value.code == "invalid_request" and error.value.message == message
    assert len(fake.calls) == before


def test_action_parses_json_string_and_denies_dmf():
    fake = Fake()
    client = make(fake)
    fake.query_reply = Response(200, json.dumps({"value": json.dumps({"status": "ok"})}))
    result = client.action("BatchJobs", "SetBatchJobToWaiting", {"batchJobId": 123})
    assert result == {"value": {"status": "ok"}, "truncated": False}
    url, _, body, _, opts = fake.calls[-1]
    assert url.endswith("/data/BatchJobs/Microsoft.Dynamics.DataEntities.SetBatchJobToWaiting")
    assert json.loads(body) == {"batchJobId": 123} and opts["method"] == "POST"
    before = len(fake.calls)
    with pytest.raises(Refused):
        client.action("DataManagementDefinitionGroups", "ImportFromPackage", {})
    assert len(fake.calls) == before


def test_reader_is_refused_write_before_call(monkeypatch, tmp_path):
    from boring_service_kit import access
    monkeypatch.setattr(access, "calling_caller", lambda: access.Caller("issuer", "alice", "alice", "reader"))
    fake = Fake()
    from mcp.server.auth.settings import AuthSettings
    auth = AuthSettings(issuer_url="https://issuer.example", resource_server_url="https://service.example",
                        validate_token_resource=True)
    server = create_server(make(fake), tmp_path, token_verifier=object(), auth=auth)
    error, result = invoke(server, "odata_write", {"entity": "Customers", "op": "create", "values": {"Name": "a"}})
    assert error and result["code"] == "forbidden" and not fake.calls
    error, result = invoke(server, "odata_action", {"entity": "BatchJobs", "action": "SetBatchJobToWaiting"})
    assert error and result["code"] == "forbidden" and not fake.calls
    error, result = invoke(server, "odata_entities", {})
    assert not error and result["shown"] == 2
    monkeypatch.setattr(access, "calling_caller", lambda: access.Caller("issuer", "bob", "bob", "writer"))
    fake.query_reply = Response(200, '{"value":"accepted"}')
    error, result = invoke(server, "odata_action", {"entity": "BatchJobs", "action": "SetBatchJobToWaiting"})
    assert not error and result["value"] == "accepted" and fake.calls[-1][4]["method"] == "POST"
    fake.query_reply = Response(201, '{"Account":"A"}')
    error, result = invoke(server, "odata_write", {"entity": "Customers", "op": "create",
                                                   "values": {"Account": "A"}})
    assert not error and result["record"]["Account"] == "A" and fake.calls[-1][4]["method"] == "POST"


def test_default_company_only_applies_to_company_specific_sets():
    fake = Fake()
    client = make(fake, company="usmf")
    client.query("BatchJobs")
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.calls[-1][0]).query)
    assert params == {}
    assert fake.calls[-1][1]["Prefer"] == "odata.maxpagesize=20"
    client.query("Customers")
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.calls[-1][0]).query)
    assert params["$filter"] == ["dataAreaId eq 'usmf'"]
    assert params["cross-company"] == ["true"]
    client.query("Customers", cross_company=True)
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.calls[-1][0]).query)
    assert params == {"cross-company": ["true"]}
    client.query("Customers", company="demf", cross_company=True)
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.calls[-1][0]).query)
    assert params["$filter"] == ["dataAreaId eq 'demf'"]
    with pytest.raises(Refused):
        client.query("BatchJobs", company="usmf")
    fake.query_reply = Response(200, '{"BatchJobId":123}')
    result = client.write("BatchJobs", "update", key={"BatchJobId": 123}, values={"BatchJobId": 123})
    assert result["record"]["BatchJobId"] == 123
    assert "cross-company" not in fake.calls[-1][0]
    with pytest.raises(Refused):
        client.write("BatchJobs", "update", key={"BatchJobId": 123}, values={"BatchJobId": 123},
                     company="usmf")


def test_next_is_followed_through_server_and_preserves_url(tmp_path):
    fake = Fake()
    server = create_server(make(fake), tmp_path)
    link = "https://aos.example/data/Customers?$skiptoken=a%2Bb"
    fake.query_reply = Response(200, json.dumps({"value": [{"Account": "A1"}], "@odata.nextLink": link}))
    error, first = invoke(server, "odata_query", {"entity": "Customers"})
    assert not error and first["next"] == link
    page = [{"Account": f"A{i}"} for i in range(25)]
    fake.query_reply = Response(200, json.dumps({"value": page}))
    before = len(fake.calls)
    error, result = invoke(server, "odata_query", {"next": first["next"]})
    assert not error and result["rows"] == page and not result["truncated"]
    assert len(fake.calls) == before + 1
    assert fake.calls[-1][0] == link and fake.calls[-1][4]["method"] == "GET"
    assert fake.calls[-1][4]["allow_redirects"] is False


def test_transport_does_not_follow_redirects(monkeypatch):
    import io
    import urllib.error
    from boring_pondnt import d365_entra
    seen = []
    def build_opener(handler):
        class FakeOpener:
            def open(self, request, timeout):
                seen.append(request.full_url)
                assert handler.redirect_request(request, None, 302, "Found", {},
                                                "https://evil.example/") is None
                raise urllib.error.HTTPError(request.full_url, 302, "Found", {}, io.BytesIO(b""))
        return FakeOpener()
    monkeypatch.setattr(d365_entra.urllib.request, "build_opener", build_opener)
    response = d365_entra.urllib_transport("https://aos.example/data/X", {"Authorization": "Bearer secret"},
                                         None, 5, method="GET", allow_redirects=False)
    assert response.status == 302 and seen == ["https://aos.example/data/X"]


def test_continuation_repeats_server_page_size_before_token():
    fake = Fake()
    client = make(fake)
    link = "https://aos.example/data/BatchJobs?$skip=1&$top=1"
    client.query(next=link)
    assert fake.calls[-1][1]["Prefer"] == "odata.maxpagesize=1"
    before = len(fake.calls)
    for bad in ("https://aos.example/data/BatchJobs?$top=501",
                "https://aos.example/data/BatchJobs?$top=no",
                "https://aos.example/data/BatchJobs?$top=",
                "https://aos.example/data/BatchJobs?$top=1&$top=2"):
        with pytest.raises(Refused):
            client.query(next=bad)
    with pytest.raises(Refused):
        client.query(next=link, top=2)
    assert len(fake.calls) == before


@pytest.mark.parametrize(("top", "size"), [(None, 500), (11, 11)])
def test_continuation_sends_prefer_when_link_has_no_top(top, size):
    fake = Fake()
    link = "https://aos.example/data/BatchJobs?$skiptoken=cursor"
    make(fake).query(next=link, top=top)
    assert fake.calls[-1][0] == link
    assert fake.calls[-1][1]["Prefer"] == f"odata.maxpagesize={size}"


def test_redactor_keeps_odata_cursor_but_still_masks_credentials():
    from boring_pondnt.d365_tools import redact
    assert redact("https://aos.example/data/X?$skiptoken=cursor&token=secret") == (
        "https://aos.example/data/X?$skiptoken=cursor&token=***")
    assert redact({"skiptoken": "secret", "token": "secret"}) == {"skiptoken": "***", "token": "***"}


@pytest.mark.parametrize("url", [
    "https://evil.example/data/Customers",
    "https://aos.example.evil.example/data/Customers",
    "https://aos.example/dataevil/Customers",
    "https://aos.example/data/../secret",
    "https://aos.example/data/%2e%2e/secret",
    "https://aos.example/data/%5csecret",
    "https://aos.example/data/Customers#fragment",
    "https://aos.example/data/Customers\n@evil",
])
def test_next_outside_safe_prefix_refused_before_token_or_request(url):
    fake = Fake()
    client = make(fake)
    with pytest.raises(Refused):
        client.query(next=url)
    assert fake.calls == []


def test_long_continuation_links(tmp_path):
    fake = Fake()
    prefix = "https://aos.example/data/Customers?$skiptoken="
    link = prefix + "a" * (16_384 - len(prefix))
    rows = [{"Account": "A1"}]
    server = create_server(make(fake), tmp_path)
    for length in (16_384, 16_385):
        next_link = (link + "a")[:length]
        fake.query_reply = Response(200, json.dumps({"value": rows, "@odata.nextLink": next_link}))
        error, result = invoke(server, "odata_query", {"entity": "Customers"})
        assert not error and result["rows"] == rows and not result["truncated"]
        if length == 16_384:
            assert result["next"] == link
        else:
            assert result["next"] is None and "16,384" in result["note"]
    error, result = invoke(server, "odata_query", {"next": link})
    assert not error and fake.calls[-1][0] == link
    error, result = invoke(server, "odata_query", {"next": link + "a"})
    assert error and result["code"] == "invalid_request"


@pytest.mark.parametrize("char", [" ", "\t", "\x00", "\x7f", "é"])
def test_next_refuses_non_printable_ascii_before_transport(char):
    fake = Fake()
    with pytest.raises(Refused) as error:
        make(fake).query(next="https://aos.example/data/Customers?$skiptoken=a" + char)
    assert error.value.code == "invalid_request"
    assert not fake.calls


@pytest.mark.parametrize("failure", [
    http.client.InvalidURL("private"), ValueError("private"),
    UnicodeEncodeError("ascii", "é", 0, 1, "private"),
])
def test_transport_exceptions_are_clean_failures(failure):
    fake = Fake()

    def transport(url, *args, **kwargs):
        if "/data/" in url:
            raise failure
        return fake(url, *args, **kwargs)

    with pytest.raises(Failed) as error:
        make(transport).query(next="https://aos.example/data/Customers?$skip=2")
    assert error.value.code == "unreachable"
    assert "private" not in error.value.message


@pytest.mark.parametrize("failure,code,hint", [
    (TimeoutError("read operation timed out"), "timeout", False),
    (urllib.error.URLError(TimeoutError("timed out")), "timeout", False),
    (urllib.error.URLError(ConnectionRefusedError("connection refused")), "unreachable", True),
    (OSError("connection reset"), "unreachable", False),
])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_d365_network_failure_uses_the_specific_timeout_and_hint(failure, code, hint, method):
    fake = Fake()

    def transport(url, *args, **kwargs):
        if url.endswith("/data/BatchJobs"):
            raise failure
        return fake(url, *args, **kwargs)

    data = make(transport, timeout=17.5)
    with pytest.raises(Failed) as error:
        data._request("/data/BatchJobs", method=method)
    assert error.value.code == code
    assert ("First call after IIS start" in error.value.message) is hint
    assert ("may have completed" in error.value.message) is (method == "POST")
    if code == "timeout":
        assert error.value.message.startswith("D365 did not answer within 17.5 s.")


def test_query_result_budget_counts_unicode_characters():
    fake = Fake()
    rows = [{"Name": "Grüße 🌍"}]
    fake.query_reply = Response(200, json.dumps({"value": rows}))
    budget = len(json.dumps(rows, ensure_ascii=False, separators=(",", ":")))
    assert make(fake).query("Customers", max_chars=budget)["rows"] == rows
    result = make(fake).query("Customers", max_chars=budget - 1)
    assert result["truncated"] and not result["rows"]


@pytest.mark.parametrize("company,total", [(None, 417), ("usmf", 32)])
def test_counted_query_keeps_server_count_when_rows_are_cut(tmp_path, company, total):
    fake = Fake()
    link = "https://aos.example/data/Customers?$count=true&$skiptoken=a"
    fake.query_reply = Response(200, json.dumps({"@odata.count": total, "value": [{"Account": "A1"}],
                                                 "@odata.nextLink": link}))
    server = create_server(make(fake), tmp_path)
    args = {"entity": "Customers", "filter": "Name eq 'x'", "count": True,
            "top": 1, "max_chars": 2, "cross_company": True}
    if company is not None:
        args["company"] = company
    error, result = invoke(server, "odata_query", args)
    assert not error and result["count"] == total and result["next"] == link
    assert result["truncated"] and result["rows"] == []
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.calls[-1][0]).query)
    filter = "(Name eq 'x')" if company is None else "(Name eq 'x') and dataAreaId eq 'usmf'"
    assert params == {"$filter": [filter], "$count": ["true"], "cross-company": ["true"]}
    error, result = invoke(server, "odata_query", {"next": link})
    assert not error and result["count"] == total and fake.calls[-1][0] == link


def test_next_refuses_count_true_before_transport_and_accepts_count_false(tmp_path):
    fake = Fake()
    server = create_server(make(fake), tmp_path)
    link = "https://aos.example/data/Customers?$count=true&$skiptoken=a"
    error, result = invoke(server, "odata_query", {"next": link, "count": True})
    assert error and result["code"] == "invalid_request" and "continuation carries it" in result["message"]
    assert not fake.calls
    error, result = invoke(server, "odata_query", {"next": link, "count": False})
    assert not error and fake.calls[-1][0] == link


def test_truncated_metadata_names_the_setting_to_raise():
    class Big(Fake):
        def __call__(self, url, headers, body, timeout, **kwargs):
            if url.endswith("/data/$metadata"):
                return Response(200, "", truncated=True)
            return super().__call__(url, headers, body, timeout, **kwargs)
    with pytest.raises(Failed) as caught:
        make(Big(), metadata_max_bytes=1000).entities()
    assert caught.value.code == "limit" and caught.value.details["truncated"]
    assert "metadata_max_bytes" in caught.value.message and "1,000" in caught.value.message


def test_next_cannot_mix_filters():
    fake = Fake()
    client = make(fake)
    with pytest.raises(Refused):
        client.query(next="https://aos.example/data/Customers?$skip=1", company="usmf")
    assert fake.calls == []


def test_metadata_inherits_base_fields_and_key():
    fake = Fake()
    fake.metadata = METADATA.replace(
        '<EntityType Name="Customer">', '<EntityType Name="CustomerBase">'
    ).replace('</EntityType>\n<EntityType Name="Batch">',
              '</EntityType><EntityType Name="Customer" BaseType="Microsoft.Dynamics.DataEntities.CustomerBase">'
              '<Property Name="Note" Type="Edm.String"/></EntityType>\n<EntityType Name="Batch">', 1)
    client = make(fake)
    customer = client.entities(entity="Customers")
    assert customer["keys"] == ["dataAreaId", "Account"]
    assert [field["name"] for field in customer["fields"]] == ["dataAreaId", "Account", "Name", "Note"]
    assert customer["company_specific"]
    assert client.write("Customers", "delete", key={"Account": "A"}, company="USMF")["deleted"] == {
        "Account": "A", "dataAreaId": "USMF",
    }


@pytest.mark.parametrize("metadata,message", [
    (METADATA.replace("<edmx:Edmx", "<!DOCTYPE edmx:Edmx><edmx:Edmx"), "metadata contains a DOCTYPE"),
    (METADATA.replace('Name="Customer"', 'Name="Customer" BaseType="Microsoft.Dynamics.DataEntities.Customer"'),
     "unknown or cyclic base entity"),
    (METADATA.replace('Name="Customer"', 'Name="Customer" BaseType="Microsoft.Dynamics.DataEntities.Missing"'),
     "unknown or cyclic base entity"),
])
def test_unsafe_or_invalid_metadata_refused(metadata, message):
    fake = Fake()
    fake.metadata = metadata
    with pytest.raises(Failed, match=message) as error:
        make(fake).entities()
    assert error.value.code == "bad_reply"


def test_refresh_reloads_metadata_and_limit_is_configured(tmp_path):
    class UpdatedFake(Fake):
        metadata = METADATA
        def __call__(self, url, headers, body, timeout, **kwargs):
            if url.endswith("/data/$metadata"):
                self.calls.append((url, headers, body, timeout, kwargs))
                return Response(200, self.metadata)
            return super().__call__(url, headers, body, timeout, **kwargs)
    fake = UpdatedFake()
    server = create_server(make(fake, metadata_max_bytes=45678), tmp_path)
    error, result = invoke(server, "odata_entities", {"search": "New"})
    assert not error and result["rows"] == []
    fake.metadata = METADATA.replace('Name="BatchJobs"', 'Name="NewBatchJobs"')
    error, result = invoke(server, "odata_entities", {"search": "New"})
    assert not error and result["rows"] == []
    error, result = invoke(server, "odata_entities", {"search": "New", "refresh": True})
    assert not error and result["rows"] == ["NewBatchJobs"]
    metadata_calls = [call for call in fake.calls if call[0].endswith("/data/$metadata")]
    assert len(metadata_calls) == 2 and all(call[4]["max_bytes"] == 45678 for call in metadata_calls)
    for invalid in (0, -1, True, 268435457, "large"):
        with pytest.raises(ValueError):
            Config("https://aos.example", "tenant", "client", "secret", metadata_max_bytes=invalid)


def test_config_default_and_scope_override():
    config = Config("https://aos.example/", "tenant", "client", "secret")
    assert config.scope == "https://aos.example/.default"
    assert Config("https://aos.example", "tenant", "client", "secret", scope="client/.default").scope == "client/.default"
    with pytest.raises(ValueError):
        Config("http://aos.example", "tenant", "client", "secret")


def test_a_server_error_after_a_write_says_it_may_have_completed():
    fake = Fake()
    client = make(fake)
    fake.query_reply = Response(502, "Bad Gateway")
    with pytest.raises(Failed) as caught:
        client.write("Customers", "update", key={"Account": "A"}, values={"Name": "N"}, company="usmf")
    assert caught.value.code == "d365_error" and "may have completed" in caught.value.message
    with pytest.raises(Failed) as caught:
        client.query("Customers")
    assert "may have completed" not in caught.value.message  # a read changes nothing


NOTES = METADATA.replace(
    '<EntityContainer Name="Container">',
    '<EntityType Name="Note"><Key><PropertyRef Name="NoteId"/></Key><Property Name="NoteId" Type="Edm.String"/>'
    '<Property Name="dataAreaId" Type="Edm.String"/><Property Name="Text" Type="Edm.String"/></EntityType>'
    '<EntityContainer Name="Container"><EntitySet Name="Notes" EntityType="Microsoft.Dynamics.DataEntities.Note"/>')


def test_a_company_that_is_not_part_of_the_key_is_refused_for_update_and_never_goes_cross_company():
    fake = Fake()
    fake.metadata = NOTES
    client = make(fake)
    with pytest.raises(Refused) as caught:
        client.write("Notes", "update", key={"NoteId": "N1"}, values={"Text": "x"}, company="usmf")
    assert "not a key field" in caught.value.message
    fake.query_reply = Response(200, '{"NoteId":"N1"}')
    result = client.write("Notes", "update", key={"NoteId": "N1"}, values={"Text": "x"})
    assert "cross-company" not in fake.calls[-1][0] and "note" not in result


@pytest.mark.parametrize("op", ["update", "delete"])
def test_a_configured_company_outside_the_key_is_reported_as_not_applied(op):
    fake = Fake()
    fake.metadata = NOTES
    fake.query_reply = Response(200, '{"NoteId":"N1"}' if op == "update" else "")
    values = {"Text": "x"} if op == "update" else None
    result = make(fake, company="usmf").write("Notes", op, key={"NoteId": "N1"}, values=values)
    assert result["note"] == ("the configured company does not apply: dataAreaId is not part of this entity's key; "
                              "the record is addressed by its key alone")
    assert "cross-company" not in fake.calls[-1][0] and "usmf" not in fake.calls[-1][0]
    fake.query_reply = Response(200, '{"Account":"A"}')
    assert "note" not in make(fake, company="usmf").write("Customers", "update", key={"Account": "A"},
                                                          values={"Name": "N"})


def test_an_etag_makes_the_write_conditional_and_a_changed_record_a_conflict():
    fake = Fake()
    client = make(fake)
    fake.query_reply = Response(412, "")
    with pytest.raises(Failed) as caught:
        client.write("Customers", "update", key={"Account": "A"}, values={"Name": "N"}, company="usmf",
                     etag='W/"JzEsNTYzNzE0NDU3Nyc="')
    assert caught.value.code == "conflict"
    assert fake.calls[-1][1]["If-Match"] == 'W/"JzEsNTYzNzE0NDU3Nyc="'
    with pytest.raises(Refused):
        client.write("Customers", "create", values={"Account": "B"}, company="usmf", etag='W/"x"')


class Rotating(Fake):
    """Entra hands out t1, t2, ...; D365 rejects the tokens listed in `rejected`."""

    def __init__(self, rejected):
        super().__init__()
        self.issued, self.rejected = 0, set(rejected)

    def __call__(self, url, headers, body, timeout, **kwargs):
        self.calls.append((url, headers, body, timeout, kwargs))
        if "login.microsoftonline.com" in url:
            self.issued += 1
            return Response(200, json.dumps({"access_token": f"t{self.issued}", "expires_in": 3600}))
        if headers["Authorization"].removeprefix("Bearer ") in self.rejected:
            return Response(401, "")
        return Response(200, self.metadata) if url.endswith("/data/$metadata") else self.query_reply

    def d365_calls(self):
        return [call for call in self.calls if "login.microsoftonline.com" not in call[0]]


def test_get_answered_401_retries_once_with_a_fresh_token(monkeypatch):
    monkeypatch.setattr('boring_pondnt.d365_entra.FRESH_TOKEN_SECONDS', 0.0)  # an old enough token
    fake = Rotating({"t1"})
    client = make(fake)
    assert client.entities(search="jobs")["rows"] == ["BatchJobs"]
    assert fake.issued == 2 and [c[1]["Authorization"] for c in fake.d365_calls()] == ["Bearer t1", "Bearer t2"]
    client.entities(search="jobs")  # the fresh token stays cached
    assert fake.issued == 2


def test_get_answered_401_twice_fails_after_one_retry(monkeypatch):
    monkeypatch.setattr('boring_pondnt.d365_entra.FRESH_TOKEN_SECONDS', 0.0)  # an old enough token
    fake = Rotating({"t1", "t2"})
    with pytest.raises(Failed) as failure:
        make(fake).entities(search="jobs")
    assert failure.value.code == "d365_error" and failure.value.details["http_status"] == 401
    assert "after one retry with a new one" in failure.value.message
    assert len(fake.d365_calls()) == 2


def test_write_answered_401_is_not_repeated_but_drops_the_token(monkeypatch):
    monkeypatch.setattr('boring_pondnt.d365_entra.FRESH_TOKEN_SECONDS', 0.0)  # an old enough token
    fake = Rotating({"t1"})
    client = make(fake)
    client.metadata()  # t1 rejected, metadata read with t2
    fake.rejected = {"t2"}
    with pytest.raises(Failed) as failure:
        client.write("Customers", "delete", key={"Account": "A"}, company="usmf")
    assert failure.value.details["http_status"] == 401 and "a repeat uses a new one" in failure.value.message
    assert [c[4]["method"] for c in fake.d365_calls()].count("DELETE") == 1
    fake.query_reply = Response(204, "")
    client.write("Customers", "delete", key={"Account": "A"}, company="usmf")
    assert fake.d365_calls()[-1][1]["Authorization"] == "Bearer t3"


def test_a_fresh_token_rejected_at_once_is_kept_and_the_get_not_repeated():
    # a 401 for a token Entra just issued is a setup problem: no second Entra request per call
    fake = Fake()
    client = make(fake)
    fake.query_reply = Response(401, "")
    with pytest.raises(Failed):
        client.query("Customers")
    tokens = [c for c in fake.calls if "login.microsoftonline.com" in c[0]]
    assert len(tokens) == 1
