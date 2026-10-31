# Compiled references in boring-pondnt-d365

Use compiled references when the question is "what calls this method?" or
"which classes extend this one?" rather than "where does this text occur?".
The `xref` tool reads Visual Studio's `DYNAMICSXREFDB`; it does not compile
code or refresh that database. Results reflect the references available there,
so source edits and incomplete builds can leave it behind the working tree.

This read-only tool belongs to the [combined D365 server](D365_SERVICE.md),
which also has write-capable tools. Choose one of four lookups with `what`.
Arguments that do not belong to that lookup are refused with the expected
arguments.

## Lookups

The times below are earlier observations on one development box, not response
guarantees. That database held 4.9 million names and 19.6 million references
across 175 modules, schema version 1.0.0.10. Use `what: info` for the
connected environment's own counts and version.

| `what` | What | Observed time |
|---|---|---|
| `info` | version, providers, counts, the reference kinds | 0.05-0.2 s |
| `names` (`name`) | paths for a name (`CustTable` -> `/Tables/CustTable`, `/Forms/CustTable`, `Table/CustTable`...), a label ID, or a prefix (`/Classes/Sales*`) | 0.003 s warm, ~2 s cold |
| `references` (`path`) | `used_by` / `uses` of a path or label; totals by kind and by module, then rows (path, kind, line, column, module); `kind`, `module`, `members` (a class with its methods, a table with its fields), `limit`, `offset` | 0.006-0.1 s; a table with its 537 members 1.7 s; `Global` with members (143,390 references) 3.2 s |
| `hierarchy` (`name`) | `down`: every class extending (or implementing) a class or interface, by depth; `up`: base chain and interfaces | 0.006-0.03 s; RunBase (3,121 classes, 7 levels) 3-6 s |

Reference kinds (`CrossReferenceKind`, Microsoft's XReferenceProviders
assembly): call, type, implements, extends, test_call, property, attribute,
test_helper_call, tag, override. A label used in a metadata property counts as
`type`.

## First lookup

Call `xref` with `{"what": "info"}`, then resolve a name:

```json
{"what": "names", "name": "CustTable"}
```

Choose the returned path that matches the intended element; a table, form and
metadata path can share a name. Pass that exact path to `references`:

```json
{"what": "references", "path": "/Tables/CustTable", "direction": "used_by", "limit": 20}
```

`used_by` asks what refers to the element; `uses` asks what it refers to.
Add `members: true` only when the question includes its methods or fields;
that expands the query. Use `limit` and `offset` for bounded reference pages.

## The data, as found

- Two providers: `Xppc.exe` (code, paths like
  `/Classes/SalesFormLetter/Methods/run`) and `Metadata` (properties, paths
  like `Table/SalesLine/TableRelation/CustTable?RelatedTable` -- after `?` the
  property). Labels are names too: `/Labels/@SYS7149`.
- Lines are lines in the element's whole source file, as in Visual Studio.
- **Orphans:** about half the references to `/Tables/CustTable` (7,354 of
  14,790) come from source ids that no longer exist in `Names`. Visual
  Studio cannot show them either; the tools join on existing names and count
  only those.
- `@SYS7149`: 193 references here (179 metadata, 14 code) against 357 textual
  uses in the source index -- the xref counts compiled uses in this
  environment's models; the source index counts every `@SYS7149` in the text.
  Both are right about different things.

## Run

Use an existing authenticated service configuration; `d365.json` below stands
for its path. Do not start a second copy on a port already owned by setup.

    .\boring-pondnt-d365.cmd serve --config d365.json
    .\boring-pondnt-d365.cmd serve --config d365.json --xref-server <instance> --xref-database <db> --xref-driver "ODBC Driver 17 for SQL Server"

HTTP only (loopback with a key, or the Pond); the same server also
has write/admin D365 tools. An old read-only xref connection must be explicitly
reviewed and retired, never silently redirected. See
[D365 configuration and ownership](D365_SERVICE.md#startup-and-sql-configuration).
