"""Bounded OData reads for a D365 environment."""
from __future__ import annotations

import http.client
import json
import math
import re
import threading
import time
import uuid
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from boring_service_kit.outcomes import Failed, Refused
from .d365_entra import EntraError, TokenSource, Transport, read_dpapi_secret, urllib_transport
from .d365data_packages import MAX_PACKAGE_BYTES, package_path as checked_package_path, read_package, save_package

WRITE_OPERATIONS = ("create", "update", "delete")
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_METADATA_BYTES = 128 * 1024 * 1024
MAX_METADATA_BYTES = 256 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_DOCTYPE = re.compile(r"<!DOCTYPE", re.IGNORECASE)
MAX_QUERY_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_BODY_BYTES = 256 * 1024
RESULT_CHARS_DEFAULT, RESULT_CHARS_MAX = 32_768, 262_144
MAX_FIELDS = 500
MAX_SETS = 500
MAX_DMF_ERRORS = 500
MAX_TEXT = 4000
MAX_NEXT_CHARS = 16_384
# D365 10.0.49 ApplicationFoundation/AxEdt: DMFDefinitionGroupName.xml:10,
# DMFPackageName.xml:8, DMFExecutionId.xml:11 (sourcecache snapshot 5ff33b1f).
# DataAreaId has no XML in that cache; live AxDB DATAAREA.ID has length 4.
DMF_TEXT_LIMITS = {"definition_group": 60, "package_name": 100, "execution_id": 90, "company": 4}
EDM = "{http://docs.oasis-open.org/odata/ns/edm}"


@dataclass
class Config:
    aos_url: str
    tenant: str
    client_id: str
    secret_file: str
    scope: str = ""
    company: str = ""
    timeout: float = 60.0
    package_folder: str = ""
    metadata_max_bytes: int = DEFAULT_METADATA_BYTES
    dmf_submit_timeout: float = 600.0

    def __post_init__(self) -> None:
        self.aos_url = self.aos_url.rstrip("/")
        parsed = urllib.parse.urlsplit(self.aos_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("aos_url must be an HTTPS URL without credentials, query or fragment")
        if not self.tenant or not self.client_id or not self.secret_file:
            raise ValueError("tenant, client_id and secret_file are required")
        for name in ("timeout", "dmf_submit_timeout"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")
        if type(self.metadata_max_bytes) is not int or not 1 <= self.metadata_max_bytes <= MAX_METADATA_BYTES:
            raise ValueError(f"metadata_max_bytes must be 1-{MAX_METADATA_BYTES}")
        if not self.scope:
            self.scope = self.aos_url + "/.default"

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return cls(**data)


def _name(value: str, kind: str) -> str:
    if not isinstance(value, str) or len(value) > 200 or not NAME.fullmatch(value):
        raise Refused("invalid_request", f"invalid {kind}")
    return value


def _text(value: str | None, kind: str) -> str | None:
    if value is not None and (not isinstance(value, str) or len(value) > MAX_TEXT):
        raise Refused("invalid_request", f"{kind} must be text of at most {MAX_TEXT} characters")
    return value


def _validate_company(company: str | None) -> None:
    _text(company, "company")
    if company == "":
        raise Refused("invalid_request", "company must not be empty; omit it, or use cross_company for queries")


def _validate_filter_balance(filter: str) -> None:
    depth = 0
    quoted = False
    for char in filter:
        if char == "'":
            # A doubled quote toggles twice without changing the quoted state.
            quoted = not quoted
        elif not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    break
    if depth or quoted:
        raise Refused("invalid_request", "filter has unbalanced parentheses or quotes")


def _check_max_chars(max_chars: int) -> None:
    if type(max_chars) is not int or not 1 <= max_chars <= RESULT_CHARS_MAX:
        raise Refused("invalid_request", f"max_chars must be 1-{RESULT_CHARS_MAX}")


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _bounded(rows: list[Any], limit: int, max_chars: int, **extra: Any) -> dict[str, Any]:
    shown: list[Any] = []
    size = 2  # Compact JSON array brackets.
    for row in rows[:limit]:
        n = _json_size(row) + (1 if shown else 0)
        if size + n > max_chars:
            break
        shown.append(row)
        size += n
    return {"rows": shown, "shown": len(shown), "truncated": len(shown) < len(rows), **extra}


def _limit_reply(result: dict[str, Any], field: str, max_chars: int) -> dict[str, Any]:
    if result[field] is not None and _json_size(result[field]) > max_chars:
        return {field: None, "truncated": True, "note": "D365 response exceeded the result limit"}
    return result


def _add_note(result: dict[str, Any], note: str | None) -> dict[str, Any]:
    if note:
        result["note"] = f"{result['note']}. {note}" if "note" in result else note
    return result


def _bounded_error_text(text: str, max_chars: int) -> str:
    # Find the longest prefix that fits after JSON escaping.
    low, high = 0, min(len(text), MAX_TEXT)
    while low < high:
        middle = (low + high + 1) // 2
        if _json_size(text[:middle]) <= max_chars:
            low = middle
        else:
            high = middle - 1
    return text[:low]


_ERROR_URL = r"""https?://[^\s"'<>\\]+"""
_ERROR_CREDENTIAL = r"""[^\s"'<>\\,;&]+"""
_ERROR_AUTH = re.compile(
    rf"\b(Authorization[ \t]*:[ \t]*)(?:(?:Basic|Bearer|Digest|Negotiate|NTLM)[ \t]+)?{_ERROR_CREDENTIAL}"
    rf"|\b(Bearer[ \t]+){_ERROR_CREDENTIAL}"
    rf"|\b(sig=){_ERROR_CREDENTIAL}", re.IGNORECASE)


def mask_error(value: Any, *, omit_all_urls: bool = False) -> Any:
    """Mask credential material in diagnostic text, preserving its structure."""
    if isinstance(value, str):
        value = re.sub(_ERROR_URL, lambda m: "[URL omitted]" if omit_all_urls or "?" in m[0] else m[0],
                       value, flags=re.IGNORECASE)
        return _ERROR_AUTH.sub(lambda m: next(group for group in m.groups() if group is not None) + "***", value)
    if isinstance(value, list):
        return [mask_error(item, omit_all_urls=omit_all_urls) for item in value]
    if isinstance(value, dict):
        return {key: mask_error(item, omit_all_urls=omit_all_urls) for key, item in value.items()}
    return value


def _error_fields(error: dict[str, Any], *, inner: bool = False) -> dict[str, Any]:
    fields = ("message", "type") if inner else ("code", "message")
    result = {field: error[field] for field in fields if isinstance(error.get(field), str)}
    child = "internalexception" if inner else "innererror"
    if isinstance(error.get(child), dict):
        result[child] = _error_fields(error[child], inner=True)
    return result


def _has_path_separator(value: Any) -> bool:
    return isinstance(value, str) and ("/" in value or "\\" in value)


def _key_literal(name: str, value: Any, typ: str, enums: dict[str, tuple[str, set[str]]]) -> str:
    if typ == "Edm.String" and isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if typ == "Edm.Guid" and isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
        return value
    if typ in {"Edm.Int16", "Edm.Int32", "Edm.Int64", "Edm.Byte", "Edm.SByte"} and type(value) is int:
        return str(value)
    if typ == "Edm.Boolean" and type(value) is bool:
        return str(value).lower()
    if typ == "Edm.Decimal":
        if type(value) is int or type(value) is float and math.isfinite(value):
            return format(Decimal(str(value)), "f")
        if isinstance(value, str) and re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
            return value
    if isinstance(value, str):
        try:
            if typ == "Edm.Date" and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                date.fromisoformat(value)
                return value
            if typ == "Edm.DateTimeOffset" and re.fullmatch(
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}"
                    r"(?::[0-9]{2}(?:\.[0-9]{1,12})?)?(?:Z|[+-][0-9]{2}:[0-5][0-9])", value):
                # Validate the calendar and offset without rounding the literal's fractional seconds.
                stamp = re.sub(r"\.[0-9]+", "", value).replace("Z", "+00:00")
                datetime.fromisoformat(stamp)
                return value
        except ValueError:
            pass
        if typ in enums and NAME.fullmatch(value):
            qualified, members = enums[typ]
            if not members or value in members:
                return f"{qualified}'{value}'"
    raise Refused("invalid_request", f"unsupported or invalid key value for {typ}", field=name, type=typ)


class DataClient:
    def __init__(self, config: Config, transport: Transport = urllib_transport,
                 secret: Callable[[], str] | None = None) -> None:
        self.config, self.transport = config, transport
        self.tokens = TokenSource(config.tenant, config.client_id, config.scope, transport,
                                  secret or (lambda: read_dpapi_secret(config.secret_file)))
        self._metadata: dict[str, dict[str, Any]] | None = None
        self._metadata_lock = threading.Lock()
        self._import_lock = threading.Lock()

    def _timeout(self, deadline: float | None) -> float:
        if deadline is None:
            return self.config.timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Failed("timeout", "the call deadline expired")
        return remaining

    def _request(self, path: str, method: str = "GET", body: bytes | None = None,
                 max_bytes: int = MAX_RESPONSE_BYTES, accept: str = "application/json",
                 page_size: int | None = None, *, deadline: float | None = None,
                 if_match: str | None = None) -> str:
        if deadline is None:  # one bound for the token and the request: a call never outlives its budget
            deadline = time.monotonic() + self.config.timeout
        uncertainty = " The operation may have completed; inspect D365 before retrying." if method != "GET" else ""
        for attempt in (1, 2):
            try:
                token = self.tokens.get(deadline=deadline)
            except EntraError as error:
                raise Failed(error.code, mask_error(error.message), **error.details) from None
            headers = {"Authorization": f"Bearer {token}", "Accept": accept}
            if page_size is not None:
                headers["Prefer"] = f"odata.maxpagesize={page_size}"
            if if_match is not None:
                headers["If-Match"] = if_match
            if body is not None:
                headers["Content-Type"] = "application/json"
            timeout = self._timeout(deadline)
            try:
                response = self.transport(self.config.aos_url + path, headers, body, timeout,
                                          method=method, max_bytes=max_bytes, allow_redirects=False)
            except OSError as error:
                cause = error.reason if isinstance(error, urllib.error.URLError) else error
                if isinstance(cause, TimeoutError):
                    raise Failed("timeout", f"D365 did not answer within {round(timeout, 1):g} s." + uncertainty) from None
                hint = " First call after IIS start may take minutes." if isinstance(cause, ConnectionRefusedError) else ""
                raise Failed("unreachable", f"D365 request failed: {error}.{hint}{uncertainty}") from None
            except (http.client.HTTPException, ValueError):
                raise Failed("unreachable", "D365 transport rejected the request or response." + uncertainty) from None
            if response.status != 401:
                break
            # A cached token can be revoked before it expires. Drop it; a GET is repeated once with a
            # fresh one, other methods are not repeated here (the caller decides) but the next call
            # gets a fresh token.
            if not self.tokens.invalidate(token) or method != "GET":
                break
        if response.truncated:
            if page_size is not None:
                raise Failed("limit", "D365's page was larger than 32 MiB; repeat with a smaller top or a narrower select",
                             truncated=True)
            uncertainty = "; the operation may have completed" if method != "GET" else ""
            raise Failed("limit", "D365 response exceeded the byte limit" + uncertainty, truncated=True)
        if not 200 <= response.status < 300:
            message = f"D365 returned HTTP {response.status}"
            try:
                error = json.loads(response.text).get("error")
                if isinstance(error, dict):
                    message += ": " + json.dumps(mask_error(_error_fields(error)), ensure_ascii=False)[:MAX_TEXT]
            except (ValueError, AttributeError, TypeError, RecursionError):
                pass
            if response.status == 412 and if_match is not None:
                raise Failed("conflict", "the record changed since it was read (its etag no longer matches); "
                             "nothing was written: read it again and decide", http_status=412)
            if method != "GET" and response.status >= 500:
                # a gateway or server error can come after D365 committed the write
                message += "." + uncertainty
            if response.status == 401:
                message += (". The Entra token was rejected and discarded"
                            + ("; a repeat uses a new one" if method != "GET" else " after one retry with a new one"))
            raise Failed("d365_error", message, http_status=response.status)
        return response.text

    def metadata(self, refresh: bool = False) -> dict[str, dict[str, Any]]:
        with self._metadata_lock:
            if refresh:
                self._metadata = None
            if self._metadata is not None:
                return self._metadata
            try:
                text = self._request("/data/$metadata", max_bytes=self.config.metadata_max_bytes,
                                     accept="application/xml")
            except Failed as error:
                if error.code == "limit" and error.details.get("truncated"):
                    raise Failed("limit", f"D365 metadata exceeded metadata_max_bytes "
                                 f"({self.config.metadata_max_bytes:,} bytes); raise metadata_max_bytes in "
                                 f"d365data.json (at most {MAX_METADATA_BYTES:,})", truncated=True) from None
                raise
            if _DOCTYPE.search(text):  # no upper-cased copy of up to 128 MiB
                raise Failed("bad_reply", "metadata contains a DOCTYPE")
            try:
                root = ET.fromstring(text)
                types: dict[str, dict[str, Any]] = {}
                enums: dict[str, tuple[str, set[str]]] = {}
                for schema in root.iter(EDM + "Schema"):
                    ns = schema.get("Namespace", "")
                    alias = schema.get("Alias")
                    for enum in schema.findall(EDM + "EnumType"):
                        name = enum.get("Name", "")
                        qualified = ns + "." + name
                        if not all(NAME.fullmatch(part) for part in qualified.split(".")):
                            raise ValueError("invalid enum type name")
                        members = {m.get("Name", "") for m in enum.findall(EDM + "Member")}
                        enums[qualified] = (qualified, members)
                        if alias:
                            enums[alias + "." + name] = (qualified, members)
                    for entity in schema.findall(EDM + "EntityType"):
                        qualified = ns + "." + entity.get("Name", "")
                        keys = [x.get("Name") for x in entity.findall("./" + EDM + "Key/" + EDM + "PropertyRef")]
                        fields = [{"name": x.get("Name"), "type": x.get("Type"), "nullable": x.get("Nullable") != "false"}
                                  for x in entity.findall(EDM + "Property")]
                        types[qualified] = {"keys": keys, "fields": fields, "base": entity.get("BaseType")}
                sets: dict[str, dict[str, Any]] = {}
                for entry in root.iter(EDM + "EntitySet"):
                    name, typename = entry.get("Name"), entry.get("EntityType")
                    if not name or typename not in types:
                        raise ValueError("entity set has no known type")
                    parts, seen = [], set()
                    current = typename
                    while current:
                        if current in seen or current not in types:
                            raise ValueError("unknown or cyclic base entity")
                        seen.add(current)
                        parts.append(types[current])
                        current = types[current]["base"]
                    fields = [f for part in reversed(parts) for f in part["fields"]]
                    keys = next((part["keys"] for part in parts if part["keys"]), [])
                    key_enums = {f["type"]: enums[f["type"]] for f in fields
                                 if f["name"] in keys and f["type"] in enums}
                    sets[name] = {"entity": name, "type": typename, "keys": keys, "fields": fields,
                                  "key_enums": key_enums,
                                  "company_specific": any(str(f["name"]).lower() == "dataareaid" for f in fields)}
                if not sets:
                    raise ValueError("no entity sets")
            except (ET.ParseError, ValueError) as error:
                raise Failed("bad_reply", f"cannot parse OData metadata: {error}") from None
            self._metadata = sets
            return sets

    def _entity(self, entity: str) -> dict[str, Any]:
        _name(entity, "entity set")
        result = self.metadata().get(entity)
        if result is None:
            raise Refused("not_found", "entity set not found", entity=entity)
        return result

    def entities(self, search: str | None = None, entity: str | None = None,
                 refresh: bool = False, *, max_chars: int = RESULT_CHARS_DEFAULT) -> dict[str, Any]:
        _check_max_chars(max_chars)
        _text(search, "search")
        if entity:
            _name(entity, "entity set")
        metadata = self.metadata(refresh=refresh)
        if entity:
            info = metadata.get(entity)
            if info is None:
                raise Refused("not_found", "entity set not found", entity=entity)
            result = _bounded(info["fields"], MAX_FIELDS, max_chars)
            result["fields"] = result.pop("rows")
            if result["truncated"]:
                shown = result["shown"]
                advice = "raise max_chars" if shown < MAX_FIELDS else f"the field limit is {MAX_FIELDS}"
                result["note"] = f"Only the first {shown} of {len(info['fields'])} fields fit; {advice}."
            return {**{k: v for k, v in info.items() if k != "key_enums"}, **result}
        names = sorted(name for name in metadata if not search or search.lower() in name.lower())
        result = _bounded(names, MAX_SETS, max_chars)
        if result["truncated"]:
            shown = result["shown"]
            advice = "narrow the search or raise max_chars" if shown < MAX_SETS else "narrow the search"
            result["note"] = f"Only the first {shown} of {len(names)} entity sets fit; {advice}."
        return result

    def query(self, entity: str | None = None, filter: str | None = None, select: str | None = None,
              order_by: str | None = None, top: int | None = None, company: str | None = None,
              cross_company: bool = False, next: str | None = None, *,
              max_chars: int = RESULT_CHARS_DEFAULT, include_etag: bool = False,
              count: bool | None = None) -> dict[str, Any]:
        _check_max_chars(max_chars)
        if count is not None and type(count) is not bool:
            raise Refused("invalid_request", "count must be a boolean")
        if top is not None and (not isinstance(top, int) or isinstance(top, bool) or not 1 <= top <= 500):
            raise Refused("invalid_request", "top must be 1-500")
        for kind, value in (("filter", filter), ("select", select), ("order_by", order_by)):
            _text(value, kind)
        _validate_company(company)
        if next is not None:
            if count:  # count=false asks for nothing the link does not already carry
                raise Refused("invalid_request", "next cannot be combined with count=true; the continuation carries it")
            if not isinstance(next, str) or len(next) > MAX_NEXT_CHARS:
                raise Refused("invalid_request", f"next must be text of at most {MAX_NEXT_CHARS:,} characters")
            prefix = self.config.aos_url + "/data/"
            if not next.startswith(prefix) or "\\" in next or "#" in next or any(not 33 <= ord(c) < 127 for c in next):
                raise Refused("invalid_request", "next must be a continuation URL under the configured AOS /data/")
            path_part = urllib.parse.urlsplit(next).path
            decoded_path = urllib.parse.unquote(path_part)
            if "%" in decoded_path or "\\" in decoded_path or any(part in {".", ".."} for part in decoded_path.split("/")):
                raise Refused("invalid_request", "next contains an unsafe path")
            if entity is not None or filter is not None or select is not None or order_by is not None or company is not None or cross_company:
                raise Refused("invalid_request", "next cannot be combined with entity or query options")
            options = urllib.parse.parse_qs(urllib.parse.urlsplit(next).query, keep_blank_values=True)
            top_values = options.get("$top")
            if top_values is not None:
                if len(top_values) != 1 or not top_values[0].isascii() or not top_values[0].isdigit():
                    raise Refused("invalid_request", "next has an invalid page size")
                digits = top_values[0].lstrip("0")
                page_size = int(digits) if 0 < len(digits) <= 3 else 0
                if not 1 <= page_size <= 500 or (top is not None and top != page_size):
                    raise Refused("invalid_request", "next page size must be 1-500 and match top")
            else:
                page_size = top if top is not None else 500
            path = next[len(self.config.aos_url):]
            return self._query_path(path, page_size, max_chars, include_etag)
        if entity is None:
            raise Refused("invalid_request", "entity or next is required")
        if filter:
            _validate_filter_balance(filter)
        info = self._entity(entity)
        if company is not None and not info["company_specific"]:
            raise Refused("invalid_request", "company was specified for an entity without dataAreaId")
        # An explicit cross_company without a company reads all companies; the config default does not apply.
        default = "" if cross_company else self.config.company
        selected_company = (company if company is not None else default) if info["company_specific"] else ""
        clauses = [f"({filter})"] if filter else []
        if selected_company:
            literal = selected_company.replace("'", "''")
            clauses.append(f"dataAreaId eq '{literal}'")
            cross_company = True
        top = top if top is not None else 20
        params: dict[str, str] = {}
        if clauses:
            params["$filter"] = " and ".join(clauses)
        if select:
            params["$select"] = select
        if order_by:
            params["$orderby"] = order_by
        if cross_company:
            params["cross-company"] = "true"
        if count:
            params["$count"] = "true"
        path = "/data/" + urllib.parse.quote(entity, safe="") + "?" + urllib.parse.urlencode(params)
        return self._query_path(path, top, max_chars, include_etag)

    def _query_path(self, path: str, top: int, max_chars: int, include_etag: bool) -> dict[str, Any]:
        try:
            data = json.loads(self._request(path, max_bytes=MAX_QUERY_RESPONSE_BYTES, page_size=top))
            rows = data["value"]
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("value is not a list of objects")
            count = data.get("@odata.count")
            if count is not None and (type(count) is not int or count < 0):
                raise ValueError("invalid @odata.count")
            next_page = data.get("@odata.nextLink")
            if next_page is not None and not isinstance(next_page, str):
                raise ValueError("invalid nextLink")
        except (ValueError, KeyError, TypeError) as error:
            raise Failed("bad_reply", f"invalid OData query response: {error}") from None
        if not include_etag:
            for row in rows:
                row.pop("@odata.etag", None)
        oversized_link = next_page is not None and len(next_page) > MAX_NEXT_CHARS
        result = _bounded(rows, top, max_chars, next=None if oversized_link else next_page)
        if count is not None:
            result["count"] = count
        shown = result["shown"]
        if oversized_link:
            kept = f"Only the first {shown} of {len(rows)} rows fit. " if result["truncated"] else ""
            result["note"] = (f"{kept}D365 sent a next link longer than {MAX_NEXT_CHARS:,} characters; "
                              "repeat with a narrower query to get any remaining rows.")
        elif next_page and result["truncated"]:
            result["note"] = (
                f"Only the first {shown} of {len(rows)} rows on this page fit; "
                "the next link continues after the whole page. "
                "Repeat with a smaller top, a narrower select, or a larger max_chars to get the rest."
            )
        elif result["truncated"]:
            advice = (f"top={shown} (or smaller), a narrower select, or a larger max_chars, and page with next."
                      if shown else "a narrower select or a larger max_chars, then page with next if available.")
            result["note"] = (
                f"Only the first {shown} of {len(rows)} rows fit and D365 sent no next link; repeat with {advice}"
            )
        return result

    def write(self, entity: str, op: str, key: dict[str, Any] | None = None,
              values: dict[str, Any] | None = None, company: str | None = None, *,
              max_chars: int = RESULT_CHARS_DEFAULT, include_etag: bool = False,
              etag: str | None = None) -> dict[str, Any]:
        _check_max_chars(max_chars)
        if op not in WRITE_OPERATIONS:
            raise Refused("invalid_request", "op must be create, update or delete")
        if etag is not None:
            if op == "create":
                raise Refused("invalid_request", "etag is for update and delete (the record's @odata.etag)")
            if not isinstance(etag, str) or not etag or len(etag) > 512 or any(not 32 <= ord(c) < 127 for c in etag):
                raise Refused("invalid_request", "etag must be the record's @odata.etag text")
        _validate_company(company)
        if key is not None and not isinstance(key, dict) or values is not None and not isinstance(values, dict):
            raise Refused("invalid_request", "key and values must be objects")
        if op == "create" and key:
            raise Refused("invalid_request", "create does not take a key")
        if op != "create" and not key:
            raise Refused("invalid_request", "update and delete require a key")
        if op == "delete" and values:
            raise Refused("invalid_request", "delete does not take values")
        if op != "delete" and not values:
            raise Refused("invalid_request", "create and update require values")
        requested_company = company if company is not None else self.config.company
        if op != "create" and (_has_path_separator(requested_company)
                               or any(_has_path_separator(value) for value in (key or {}).values())):
            raise Refused("invalid_request",
                          "D365 OData cannot address a key containing / or \\ in the URL path; "
                          "change or delete the record in the D365 client")
        info = self._entity(entity)
        fields = {f["name"]: f["type"] for f in info["fields"]}
        if company is not None and not info["company_specific"]:
            raise Refused("invalid_request", "company was specified for an entity without dataAreaId")
        chosen_company = requested_company if info["company_specific"] else ""
        payload = dict(values or {})
        keys = dict(key or {})
        for name in list(payload) + list(keys):
            _name(name, "field")
            if name not in fields:
                raise Refused("invalid_request", "unknown entity field", field=name)
        for name, value in payload.items():
            if isinstance(value, (dict, list)):
                raise Refused("invalid_request", "nested values are not supported", field=name)
        company_field = next((f for f in fields if f.lower() == "dataareaid"), None)
        company_note = None
        if chosen_company and company_field:
            for mapping in (keys, payload):
                if company_field in mapping:
                    value = mapping[company_field]
                    if not isinstance(value, str) or value.casefold() != chosen_company.casefold():
                        raise Refused("invalid_request", "company conflicts with dataAreaId")
            if op == "create":
                payload[company_field] = chosen_company
            elif company_field in info["keys"]:
                keys[company_field] = chosen_company
            elif company is not None:
                # cross-company with a key without dataAreaId could address another company's record
                raise Refused("invalid_request", "company cannot be applied: dataAreaId is not a key field of this "
                              "entity; omit company and give the record's own key", keys=info["keys"])
            else:
                # Say so rather than let the caller believe the configured company scoped the write.
                company_note = ("the configured company does not apply: dataAreaId is not part of this entity's key; "
                                "the record is addressed by its key alone")
        path = "/data/" + urllib.parse.quote(entity, safe="")
        if op != "create":
            if not info["keys"] or set(keys) != set(info["keys"]):
                raise Refused("invalid_request", "key must contain every entity key field", required=info["keys"])
            parts = []
            for name in info["keys"]:
                literal = _key_literal(name, keys[name], fields[name], info["key_enums"])
                parts.append(_name(name, "key field") + "=" + urllib.parse.quote(literal, safe="'"))
            path += "(" + ",".join(parts) + ")"
        if op != "create" and company_field and company_field in info["keys"] and keys.get(company_field):
            path += "?cross-company=true"  # the key names its company: address it there, not in the user's default
        body = None if op == "delete" else self._body(payload)
        text = self._request(path, method={"create": "POST", "update": "PATCH", "delete": "DELETE"}[op], body=body,
                             if_match=etag)
        if op == "delete":
            result = _limit_reply({"deleted": keys, "truncated": False}, "deleted", max_chars)
            return _add_note(result, company_note)
        result = self._reply(text, "record")
        record = result["record"]
        returned = record if isinstance(record, dict) else {}
        if not include_etag:
            returned.pop("@odata.etag", None)
        result = _limit_reply(result, "record", max_chars)
        if op == "create" and any(_has_path_separator(returned.get(name, payload.get(name))) for name in info["keys"]):
            note = "This key contains / or \\ and cannot be updated or deleted through OData. Use the D365 client."
            result = _add_note(result, note)
        return _add_note(result, company_note)

    @staticmethod
    def _body(value: Any) -> bytes:
        try:
            body = json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, RecursionError):
            raise Refused("invalid_request", "arguments must be valid JSON") from None
        if len(body) > MAX_BODY_BYTES:
            raise Refused("limit", "request body exceeds the byte limit")
        return body

    @staticmethod
    def _reply(text: str, field: str) -> dict[str, Any]:
        if not text.strip():
            return {field: None, "truncated": False, "note": "D365 returned no response body"}
        try:
            value = json.loads(text)
            if field == "value" and isinstance(value, dict) and "value" in value:
                value = value["value"]
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        pass
        except ValueError:
            raise Failed("bad_reply", "D365 returned an invalid JSON response; the operation may have completed") from None
        return {field: value, "truncated": False}


    def _dmf(self, action: str, arguments: dict[str, Any], deadline: float | None) -> Any:
        path = "/data/DataManagementDefinitionGroups/Microsoft.Dynamics.DataEntities." + action
        try:
            text = self._request(path, method="POST", body=self._body(arguments), deadline=deadline)
        except Failed as error:
            # D365 may echo packageUrl in an error.
            error.message = mask_error(error.message, omit_all_urls=True)
            raise
        try:
            value = json.loads(text)["value"]
            if isinstance(value, dict) and set(value) == {"value"}:
                value = value["value"]
            return value
        except (ValueError, KeyError, TypeError):
            raise Failed("bad_reply", "invalid DMF action response; inspect D365 before retrying") from None

    @staticmethod
    def _dmf_text(value: Any, kind: str) -> str:
        limit = DMF_TEXT_LIMITS[kind]
        if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
            raise Refused("invalid_request", f"{kind} must be nonempty text of at most {limit} characters")
        return value

    @staticmethod
    def _execution(value: Any) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > DMF_TEXT_LIMITS["execution_id"]:
            raise Failed("bad_reply", "DMF did not return an execution id; inspect D365 before retrying")
        return value

    def _blob(self, url: Any, method: str, body: bytes | None = None, *, deadline: float | None = None) -> bytes:
        # The URL comes only from D365, never a caller. Do not send an AOS token
        # to storage, follow redirects, or expose the URL through exceptions.
        try:
            parsed = urllib.parse.urlsplit(url) if isinstance(url, str) else None
            valid = (parsed is not None and (parsed.scheme == "https" or
                         parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}) and parsed.hostname
                     and not parsed.username and not parsed.password and not parsed.fragment
                     and not any(ord(c) < 33 for c in url) and "\\" not in url)
        except ValueError:
            valid = False
        if not valid:
            raise Failed("bad_reply", "DMF returned an invalid storage URL; HTTP is allowed only for literal loopback")
        headers = {"Content-Type": "application/zip", "x-ms-blob-type": "BlockBlob"} if method == "PUT" else {}
        try:
            response = self.transport(url, headers, body, self._timeout(deadline), method=method,
                                      max_bytes=MAX_PACKAGE_BYTES if method == "GET" else MAX_RESPONSE_BYTES,
                                      allow_redirects=False, binary=True)
        except (OSError, http.client.HTTPException, ValueError):
            raise Failed("storage_unreachable", "storage request failed; upload may have completed" if method == "PUT"
                         else "storage download failed") from None
        if response.truncated:
            raise Failed("limit", "storage response exceeded the byte limit", truncated=True)
        if not 200 <= response.status < 300:
            raise Failed("storage_error", f"storage returned HTTP {response.status}", http_status=response.status)
        return response.raw

    def dmf_export(self, definition_group: str, company: str, package_name: str | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.timeout
        definition_group = self._dmf_text(definition_group, "definition_group")
        company = self._dmf_text(company, "company")
        package_name = self._dmf_text(package_name if package_name is not None else definition_group, "package_name")
        value = self._dmf("ExportToPackage", {"definitionGroupId": definition_group, "packageName": package_name,
                          "executionId": "", "reExecute": False, "legalEntityId": company}, deadline)
        return {"execution_id": self._execution(value)}

    def dmf_import(self, definition_group: str, package_path: str, company: str,
                   execute: bool = True, overwrite: bool = True) -> dict[str, Any]:
        data = self.prepare_import(definition_group, package_path, company, execute, overwrite)
        return self.submit_import(definition_group, data, company, execute, overwrite)

    def prepare_import(self, definition_group: str, package_path: str, company: str,
                       execute: bool, overwrite: bool) -> bytes:
        self._dmf_text(definition_group, "definition_group")
        self._dmf_text(company, "company")
        if type(execute) is not bool or type(overwrite) is not bool:
            raise Refused("invalid_request", "execute and overwrite must be booleans")
        return read_package(self.config.package_folder, package_path)

    def submit_import(self, definition_group: str, data: bytes, company: str,
                      execute: bool, overwrite: bool) -> dict[str, Any]:
        """Submit a package already checked by prepare_import."""
        deadline = time.monotonic() + self.config.dmf_submit_timeout
        # Microsoft's package API disallows parallel ImportFromPackage calls.
        if not self._import_lock.acquire(blocking=False):
            raise Refused("busy", "another package import submission is in progress")
        try:
            blob_name = uuid.uuid4().hex + ".zip"
            location = self._dmf("GetAzureWriteUrl", {"uniqueFileName": blob_name}, deadline)
            try:
                location = json.loads(location) if isinstance(location, str) else location
                url = location["BlobUrl"]
            except (ValueError, KeyError, TypeError):
                raise Failed("bad_reply", "DMF did not return a blob upload location") from None
            uploaded = False
            try:
                self._blob(url, "PUT", data, deadline=deadline)
                uploaded = True
                value = self._dmf("ImportFromPackage", {"packageUrl": url, "definitionGroupId": definition_group,
                                  "executionId": "", "execute": execute, "overwrite": overwrite, "legalEntityId": company},
                                  deadline)
                execution_id = self._execution(value)
            except Failed as error:
                note = "A storage blob may remain. Inspect D365 before retrying. No automatic delete or retry."
                if uploaded:
                    note = "The import may have been submitted. Check D365 execution history before retrying. " + note
                error.details.update(upload_name=blob_name, note=note)
                raise
            result = {"execution_id": execution_id, "upload_name": blob_name, "package_bytes": len(data)}
            if not execute:
                result["note"] = ("D365 attached the package to the project; no execution runs, so there is nothing to poll. "
                                  "Run the project in D365, or import again with execute=true.")
            return result
        finally:
            self._import_lock.release()

    def dmf_status(self, execution_id: str, download: bool = False, *,
                   max_chars: int = RESULT_CHARS_DEFAULT) -> dict[str, Any]:
        _check_max_chars(max_chars)
        self._dmf_text(execution_id, "execution_id")
        if type(download) is not bool:
            raise Refused("invalid_request", "download must be a boolean")
        deadline = time.monotonic() + self.config.timeout  # both reads (status, errors) share the call's time
        if download:
            checked_package_path(self.config.package_folder)
        try:
            status = self._dmf("GetExecutionSummaryStatus", {"executionId": execution_id}, deadline)
        except Failed as error:
            if (error.code == "d365_error" and error.details.get("http_status") == 400
                    and "execution details were not found for execution id" in error.message.casefold()):
                raise Refused("not_found", "D365 did not find the DMF execution", execution_id=execution_id) from None
            raise
        states = {"Unknown", "NotRun", "Executing", "Succeeded", "PartiallySucceeded", "Failed", "Canceled"}
        if not isinstance(status, str) or status not in states:
            raise Failed("bad_reply", "DMF returned an unrecognized execution status")
        result: dict[str, Any] = {"execution_id": execution_id, "status": status,
                                 "terminal": status in {"Succeeded", "PartiallySucceeded", "Failed", "Canceled"}}
        try:
            errors = self._dmf("GetExecutionErrors", {"executionId": execution_id}, deadline)
        except Failed as error:
            message = _bounded_error_text(error.message, max_chars)
            result.update(errors_available=False, errors_code=error.code, errors_message=message)
            if len(message) < len(error.message):
                result["errors_truncated"] = True
            if error.details.get("http_status") in {404, 405, 501}:
                result["errors_note"] = "GetExecutionErrors is unavailable on this endpoint"
        else:
            # X++ formats the inner JSON itself; raw control characters are normal.
            raw = errors if isinstance(errors, str) else json.dumps(errors, ensure_ascii=False)
            note = "GetExecutionErrors did not return a JSON list"
            try:
                if isinstance(errors, str):
                    errors = json.loads(errors, strict=False) if errors.strip() else []
            except ValueError:
                errors = None
                note = "D365 returned error JSON that does not parse"
            result["errors_available"] = True
            if isinstance(errors, list):
                bounded = _bounded(mask_error(errors, omit_all_urls=True), MAX_DMF_ERRORS, max_chars)
                result.update(errors=bounded["rows"], errors_shown=bounded["shown"],
                              errors_truncated=bounded["truncated"])
                if bounded["truncated"]:
                    shown = bounded["shown"]
                    advice = "raise max_chars" if shown < MAX_DMF_ERRORS else f"the error limit is {MAX_DMF_ERRORS}"
                    result["errors_note"] = f"Only the first {shown} of {len(errors)} errors fit; {advice}."
            else:
                raw = mask_error(raw, omit_all_urls=True)
                text = _bounded_error_text(raw, max_chars)
                result.update(errors_text=text, errors_truncated=len(text) < len(raw), errors_note=note)
        if download:
            result["downloaded"] = False
            if status in {"Succeeded", "PartiallySucceeded"}:
                try:
                    url = self._dmf("GetExportedPackageUrl", {"executionId": execution_id}, deadline)
                    if url == "":
                        result["download_note"] = "No exported package is available for this execution"
                    else:
                        data = self._blob(url, "GET", deadline=deadline)
                        self._timeout(deadline)
                        result.update(save_package(self.config.package_folder, data), downloaded=True)
                except (Failed, Refused) as error:
                    result.update(download_code=error.code, download_error=mask_error(error.message))
            else:
                result["download_note"] = "No download attempted in this execution state"
        return result

    def action(self, entity: str, action: str, arguments: dict[str, Any] | None = None, *,
               max_chars: int = RESULT_CHARS_DEFAULT) -> dict[str, Any]:
        _check_max_chars(max_chars)
        _name(action, "action")
        if entity.lower() == "datamanagementdefinitiongroups":
            raise Refused("forbidden", "DMF actions belong to dmf tools")
        self._entity(entity)
        if arguments is not None and not isinstance(arguments, dict):
            raise Refused("invalid_request", "arguments must be an object")
        path = "/data/" + urllib.parse.quote(entity, safe="") + "/Microsoft.Dynamics.DataEntities." + action
        result = self._reply(self._request(path, method="POST", body=self._body(arguments or {})), "value")
        return _limit_reply(result, "value", max_chars)
