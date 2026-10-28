# Write Paths and Plugin-Free Operation

## Dependencies

Mandatory: Python 3.10+, Zotero Desktop running with its local API enabled and
built-in Connector server on localhost:23119. PDF uses available `pdftotext`,
otherwise installed `pypdf`/`PyPDF2`; unsupported font encoding is an explicit
failure. Readers are optional for other formats and never installed implicitly.
No Zotero cloud account, API key or third-party plugin is needed
for ordinary imports. Transfer the entire skill, including scripts/ingest.py.

Text-only citation search/review needs Python and public source access, not
Zotero. Optional provenance notes have both a schema-discovered plugin route and
a bundled plugin-free native data-API route; see
[provenance-notes.md](provenance-notes.md) for the complete operation and consent
contract. No extra installed skill is needed. Neither route modifies SQLite.

Optional: an already installed `zotero-mcp-plugin` exposing Streamable HTTP MCP
at `http://127.0.0.1:23120/mcp`. `--plugin-url` allows another loopback HTTP address.
The adapter is bundled; it does not import a Codex plugin or install anything.
The Codex Zotero plugin and this Zotero desktop extension are distinct components.

## Plugin Protocol and Operations

The adapter sends JSON-RPC MCP `initialize`, `notifications/initialized`, then
paginated `tools/list`; it preserves `Mcp-Session-Id`, accepting JSON/SSE responses.
It calls a tool only if its advertised schema accepts the fields below and has
no additional unknown required parameters. Discovery failure disables reuse;
permission denial blocks alternate writes. Tool names alone are not proof.

### Internal Library Identity

`tools/call` parameters:

```json
{"name":"get_libraries","arguments":{"limit":100,"offset":0}}
```

The inspected handler returns `{libraryID,name,libraryType}` entries. `libraryID`
is internal, not a public user/group ID. For personal imports, use the unique
user library. For a group, read its public ID/name from `/api/users/0/groups`;
accept correspondence to a plugin library only when names are unique on both
surfaces. Ambiguous names disable this optimization; never guess the internal ID.

### Add Existing Items

```json
{"name":"add_items_to_collection","arguments":{"libraryID":1,"collectionKey":"TARGETKEY","itemKeys":["ITEMKEY1"]}}
```

The inspected implementation resolves the collection and items using
`getByLibraryAndKeyAsync` in the given library, checks already-present items,
then calls **`collection.addItems(itemIDs, ...)` through Zotero transactions**.
It does not replace the item's existing collection list. It returns `success`,
`collectionKey`, `added`, `notFound`, `alreadyInCollection` and notification status.

The skill snapshots existing memberships, calls once, then re-reads the built-in
local API to confirm the same item is in target and old memberships remain.
Returned success/counts are informational only.

### Create Collection

```json
{"name":"create_collection","arguments":{"libraryID":1,"name":"Final","parentCollection":"PARENTKEY"}}
```

Omit parentCollection for a root collection. The handler creates a
`Zotero.Collection`, assigns library/name, resolves its parent in that library,
then uses `saveTx`. The skill first checks exact name+parent, calls once if absent,
then verifies by re-reading. Multiple matches or uncertain results block blind retry.

CLI wrappers, relative to the skill's scripts directory:

```bash
python zotero.py write-capabilities
python zotero.py create-collection --name "Final" --parent PARENTKEY --yes
python zotero.py import-references --source refs.xlsx --collection TARGETKEY --yes
```

Capability discovery probes schemas and Connector reading, not actual write success.

## Without the Plugin

The bundled fallback remains usable:

1. Read selected library/target items with the built-in local API.
2. Reuse a clear target match. A match elsewhere is not completion.
3. If reuse is unavailable, verify metadata and re-read target before fallback.
4. POST `{}` to `/connector/getSelectedCollection`. Its internal libraryID,
   numeric collection id, editability and hierarchical targets (`L<id>`, `C<id>`)
   are compared with read-only collection keys/parents. Require a unique full
   hierarchy and proven library identity. This request does not change selection.
5. POST sanitized UTF-8 RIS (`Content-Type: text/plain`) once to
   `/connector/import?session=codex-<unique-id>`. The native server uses
   `Zotero.Translate.Import` and the currently selected UI destination.
6. Re-read target and verify item identity and `data.collections`; check a final
   snapshot. Map to the adopted item, not the old item that remained elsewhere.

The built-in API cannot create collections or add existing items. If a new
collection is required, choose its library/parent in Zotero's collection tree,
create the collection/subcollection, select it, then re-read its key/parent.
Available authorized desktop automation may perform this; otherwise the user
does the UI step. An existing target only needs selection for Connector import.
If neither UI nor the optional writer is available, report the limitation.
Do not install the optional plugin as a prerequisite or invent an HTTP writer.
Never edit zotero.sqlite directly.

## Failure Boundaries

- Reuse unavailable/fails/no verified membership: bounded reads plus a final
  pre-import read after metadata lookup; one fallback copy if target is confirmed
  absent. Explain reason and duplicate relationship.
- Lost reply but membership proven: success without another import.
- Target unreadable or match ambiguous: pending, not fallback based on ignorance.
- Explicit permission denial: failed, no bypass with a different writer.
  This includes RPC/HTTP/tool errors saying write operations are disabled.
  A denial stops subsequent writes in the batch; already-present references
  can still be verified read-only. Generic RPC -32603 alone is not a denial.
- Import response abnormal: verify, never automatically import again.
- Wrong/unverifiable selected target or unreachable Connector: pending, explain
  the missing selection/service prerequisite.

Default verification windows use three reads (Python API allows 1-5). At most
one add and one import per identified article per invocation. CLI import and
citation import now use a durable per-target journal in the OS temporary
directory's `zotero-input-state` folder. `--journal` optionally selects a file;
`ZOTERO_INPUT_STATE_DIR` optionally changes the cache directory for isolation.
The journal binds endpoint, actual library identity and collection, recording
an attempt before writing and the verified adopted item/history afterward.
Started/unknown/denied prior attempts require read-only reconciliation rather
than an automatic repeat. Do not delete/change journals to force a retry.
The Python callable opts in with `journal_path`; preserve it across calls.
Corrupt or mismatched logs fail before writes. An exclusive sidecar lock stops
concurrent jobs using the same journal. A crash may leave a lock: inspect its
recorded PID, establish that the owner ended, and retain the journal before
removing only that stale lock. Do not clear a running owner's lock.
Output publication uses flushed temporary files and atomic publication;
filesystem failure after a Zotero write is recovered from the journal and
target reads. This is not an atomic transaction across Zotero and the filesystem.
Do not change UI selection during import; the check/import pair is not atomic.

Preference edits use `enable/disable --profile <directory>`. Optional automatic
restart additionally requires `--pid <verified PID>` on Windows and verifies
that process's executable and explicit profile argument before a graceful close.
No global taskkill/pkill is used. Unidentified/default-profile instances and
other operating systems require a manual restart of the intended instance.
These configuration arguments are not required for ordinary imports.

Approved membership removal uses the schema-discovered
`remove_items_from_collection` operation with the same libraryID/collectionKey/
itemKeys fields as add. The bundled native/UI alternative and read-back rules
are in [collection-sync.md](collection-sync.md).

## Evidence and Remaining Verification

On 2026-09-30, read-only inspection confirmed these operations in the installed
`zotero-mcp-plugin@autoagent.my.xpi`, `content/scripts/zotero-mcp-plugin.js`.
Zotero's `app/omni.ja` sources `server_connector.js`, `server_localAPI.js`,
`data/library.js` and `data/dataObject.js` confirmed native import, response
shapes and public/internal library-ID distinctions. These are evidence sources,
not hard-coded runtime dependencies of the skill.

The earlier 2026-10-03 isolated-profile probe refused collection creation with
`Write operations are currently disabled`; no alternate writer was used. Its
runtime list did not expose `write_note`. Historical extension source inspection
alone was not proof of availability.

The 2026-10-04 authorized fresh-profile test used Zotero 9.0.6 and the already
installed extension 1.6.0, with its documented write preference enabled only in
that isolated profile. Runtime discovery exposed compatible create/add/remove
schemas. Actual read-back verified root/nested collection creation, same-item
reuse into the target without losing its original membership, target-existing
reuse, and target-only removal without deleting the library item or its other
membership. Connector test-fixture import returned abnormally but the item
appeared afterward; no second import was issued. This does not verify normal
Connector import into a selected UI collection.

The same enabled profile exposed `write_note`, but one consented child-note
attempt timed out and subsequent read-only checks found no matching child.
Its durable journal remains uncertain; no retry or alternate writer was used.

On 2026-10-14 the user authorized real-paper testing in the experiment collection.
The ordinary coordinator verified that collection's current UI selection, then
imported one PubMed-verified record through Connector and independently confirmed
the actual adopted item and target membership. The advertised `write_note` route
created one child whose content, parent and membership were independently read
back. A repeated job reused the same key, without another create. These are
successful live tests in that environment, not a resolution of the old unknown
attempt. Plugin-free native-console/UI execution, real group-library writes and
profile-scoped automatic restart remain unverified. Never present isolated
state/browser tests as those integrations. Exact optional note/native operations
and local Markdown snapshots are in the notes guide.
