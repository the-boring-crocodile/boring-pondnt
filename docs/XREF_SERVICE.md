# Compiled references in boring-pondnt-d365

These tools belong to the combined D365 server
([configuration](D365_SERVICE.md)).

Visual Studio's cross-references (`DYNAMICSXREFDB`) as four read-only tools
Measured on a D365 development box: 4.9 million names,
19.6 million references, 175 modules, schema version 1.0.0.10.

| Tool | What | Typical time |
|---|---|---|
| `xref_info` | version, providers, counts, the reference kinds | 0.05-0.2 s |
| `xref_names` | paths for a name (`CustTable` -> `/Tables/CustTable`, `/Forms/CustTable`, `Table/CustTable`...), a label ID, or a prefix (`/Classes/Sales*`) | 0.003 s warm, ~2 s cold |
| `xref_references` | `used_by` / `uses` of a path or label; totals by kind and by module, then rows (path, kind, line, column, module); `kind`, `module`, `members` (a class with its methods, a table with its fields), `limit`, `offset` | 0.006-0.1 s; a table with its 537 members 1.7 s; `Global` with members (143,390 references) 3.2 s |
| `xref_hierarchy` | `down`: every class extending (or implementing) a class or interface, by depth; `up`: base chain and interfaces | 0.006-0.03 s; RunBase (3,121 classes, 7 levels) 3-6 s |

Reference kinds (`CrossReferenceKind`, Microsoft's XReferenceProviders
assembly): call, type, implements, extends, test_call, property, attribute,
test_helper_call, tag, override. A label used in a metadata property counts as
`type`.

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

    boring-pondnt-d365.cmd serve
    boring-pondnt-d365.cmd serve --xref-server <instance> --xref-database <db> --xref-driver "ODBC Driver 17 for SQL Server"

HTTP only (loopback with a key, or the Pond); the same server also
has write/admin D365 tools. An old read-only xref connection must be explicitly
reviewed and retired, never silently redirected. See D365_SERVICE.md.
