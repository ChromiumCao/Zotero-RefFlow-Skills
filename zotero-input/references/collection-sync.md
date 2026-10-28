# Optional Collection Membership Sync

Ordinary import never removes anything. For an explicit removal/sync request,
first import and verify additions, then resolve extraction gaps and ambiguous
references. Removal is limited to the requested library/collection; existing
items, other collection memberships, children and notes remain untouched.
Do not interpret unreadable/empty documents or a partial bibliography as deletions.

Commands are relative to this skill's scripts directory:

```bash
python collection_sync.py plan --source refs.xlsx --collection TARGETKEY --out removals.json
python collection_sync.py apply --plan removals.json --approve-removals --confirm-source-complete --out removed.json
python collection_sync.py verify --plan removals.json --out removal-verification.json
```

`plan` is read-only, accepts multiple `--source` flags or optional
`--references-json`, and preserves chosen library identity. Groups need
`--library-type group --library-id <PUBLIC_GROUP_ID>`. Preview source/target
counts, each exact item identity/title and its preserved memberships. Confirm
the user approved the preview and that the complete reference list was extracted;
the two apply flags represent this existing approval, not new ordinary-import
questions. More than five removals and over 25% of target items is flagged as a
large change and additionally requires `--acknowledge-large-change` after review.
Source file hashes, plan integrity and candidate snapshots must still match.
Any unmatched source, missing formula/identifier or ambiguous reference blocks
apply until resolved. Callers moving files or reordering references should carry
stable document/reference IDs through structured input; filenames/row positions
alone do not prove continuity.

The optional existing desktop plugin operation is:

```json
{"name":"remove_items_from_collection","arguments":{"libraryID":1,"collectionKey":"TARGETKEY","itemKeys":["ITEMKEY1"]}}
```

Use it only when the current runtime advertises a compatible schema. Snapshot
all candidates before any write; call once per item, then use bounded local-API
reads to verify absence from target and presence of every old other membership.
Lost responses with proven removal are success. Denied writes stop subsequent
writes without switching channels; unavailable/unverified writes remain pending.
`verify` is read-only and reruns never trash or recreate library items.

Without a compatible plugin, the bundled native route is:

```bash
python collection_sync.py native-script --plan removals.json --approve-removals --confirm-source-complete --out remove-memberships.js
```

Only choose it before any denied/uncertain plugin attempt. In the intended Zotero
profile open Tools > Developer > Run JavaScript, enable async mode and execute
the supplied script. It resolves public library identity and exact collection,
checks every item/version/title/DOI/membership before writing, and calls
`collection.removeItems([item.id])` through Zotero's native data API. It never
calls item erase/trash or SQL. Re-run the independent `verify` command afterward;
the script output alone cannot replace target and preserved-membership reads.
Available authorized desktop automation can operate the console; otherwise a
user performs this UI step. The normal UI's Remove from Collection is another
supported option for the reviewed exact list. No extra installed skill/plugin
is required. If neither execution route is available, report removals pending.

Schemas are `zotero-input.removal-plan/1` and `zotero-input.removal-results/1`.
These optional operations do not change mapping v1 or automatically synchronize
source deletion. Their simulations and generated native scripts do not establish
successful real-library integration until independently tested.
