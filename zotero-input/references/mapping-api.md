# Optional Mapping Interface

## CLI

Commands are relative to this skill's scripts directory:

```bash
# Ordinary standalone import; no mapping file
python zotero.py import-references --source refs.xlsx --collection TARGETKEY --yes

# Optional export from ordinary files
python zotero.py import-references --source a.docx --source b.md --collection TARGETKEY --yes --mapping-out mapping.json

# Pre-extracted references; mapping export is still optional
python zotero.py import-references --references-json refs.json --collection TARGETKEY --yes --mapping-out mapping.json
```

For groups, add `--library-type group --library-id <PUBLIC_GROUP_ID>`, never the
plugin-internal libraryID. Both input paths use identical reconciliation logic.
No input requires Word fields, body positions, merge order or output style.

## Structured Input v1

```json
{
  "schema_version": "zotero-input.references/1",
  "documents": [
    {
      "document_id": "chapter-a",
      "references": [
        {
          "reference_id": "ref-3",
          "raw_text": "[3] Original reference text",
          "label": "[3]",
          "doi": "10.1234/example",
          "pmid": null,
          "title": null,
          "first_author": null,
          "year": null,
          "source_location": {"caller_defined": ["paragraph-7", "citation-2"]}
        }
      ]
    }
  ]
}
```

Input schema_version is optional for v1; explicit unsupported versions are
rejected. document_id is required. reference_id is supplied or generated as
ref-000001, etc. Each `(document_id,reference_id)` pair must be unique, even if
several entries share DOI/title/item. Different documents may each have `[3]`.
Missing raw text/labels/locations are null. Supply a DOI, PMID or parsed title
with available author/year; all bibliographic fields are not mandatory.

source_location accepts opaque JSON and is copied without interpreting it.
It may contain multiple body occurrences. Put caller-specific round-trip data
there; arbitrary other fields are not guaranteed round-trip preservation.
Body-position extraction remains the caller's job.

Optional `metadata` and `metadata_provenance` let callers supply source-verified
bibliographic records without repeating online lookups. See
[chinese-references.md](chinese-references.md) for fields and checks. A metadata-only
reference is accepted when its verified record supplies title and creators;
ordinary DOI/PMID/title input remains valid without these fields. Optional
`title_aliases` inside metadata must be titles shown by the same source, not
model-generated translations. Optional `source_encoding` describes decoded file
input; it is null when unknown.

Optional known identity:

```json
{"zotero_item":{"library":{"type":"user","id":123456},"item_key":"ABCDEFGH"}}
```

It must resolve in the selected library and agree with supplied identifiers;
unresolved/wrong-library/conflicting identities stay pending. Fallback results
point to the new verified item rather than remaining pinned to this old identity.

File input uses absolute file URI as document_id and extraction-order reference
IDs. These are unique per document but not stable across edits or file moves.
Callers needing persistent source identity must provide it explicitly.

## Mapping Output v1

Top level: schema_version `zotero-input.mapping/1`, complete, library,
target_collection, counts, mappings. There is one mapping per source occurrence.

| Field | Meaning |
|---|---|
| source | document_id, reference_id, original raw_text, label, opaque source_location, source_encoding or null |
| identifiers | normalized doi, pmid, title; absent values null |
| match | evidence basis, ambiguous flag, candidates, reason/conflicts |
| item | adopted library identity, item_key, uri or null; unadopted item null |
| target_collection | library identity and collection key |
| membership_verified | actual target-read result, not a receipt |
| outcome | status enum below |
| fallback_reason | reason for fallback, or null |
| known_duplicates | known old identities; not an exhaustive duplicate audit |
| anomalies | missing/incorrect abstracts, abnormal responses, other problems |
| metadata_provenance | provenance of supplied/exported adopted metadata, or null; not a fetched-URL attestation |
| adoption_history | optional additive v1 history of original outcome, adopted item, fallback reason and known duplicates; refresh preserves it |

Outcomes:

- target_existing: clear target match verified, no write needed.
- reused_added: original item verified in target, old memberships preserved.
- new_import: library miss, new import, target membership verified.
- fallback_import: reuse unavailable/unverified; actual adopted new item mapped.
- pending: ambiguity, uncertain write/membership, missing metadata or prerequisites.
- failed: explicit permission failure; no alternate-write bypass.

complete requires every source row to have a successful status and verified
membership in the final snapshot. It does not guarantee every optional metadata
field exists; inspect anomalies. Final read failure revokes provisional success.
Do not use pending/failed rows as confirmed downstream citation bindings.

The encoding/provenance fields are additive v1 extensions. Each source occurrence
retains its own IDs, original text and locations, even when an RIS/JSON catalog
supplies metadata for it. Catalog record numbers do not replace source reference
IDs. A fallback continues to map the final verified item, not the catalog or old
library candidate. Unknown provenance remains null.
Refreshing evidence/location mappings changes current verification state but
retains fallback reasons, duplicate relationships and adoption history. It does
not reassign a prior adopted identity to another candidate. Mapping outputs
cannot overwrite sources/existing results; use a fresh output name.

Library identity is `{type,id,id_kind}`. Groups use public group IDs. Personal
libraries use the actual user ID reported by the local API when available;
otherwise id=0 with id_kind=local_user_alias explicitly indicates a local alias.
An offline user/0 plus item key is not globally unique across computers/profiles.
A future caller must resolve it in the originating library, not assume another
computer's user/0 is the same. URI is copied only if explicitly present in the
authoritative item; no Zotero/Word URI is fabricated from internal IDs or names.

## Python API

With this skill's scripts directory on the import path:

```python
from ingest import Library, LocalBackend, normalize_sources, reconcile

refs = normalize_sources(documents)  # documents array from input above
result = reconcile(refs, LocalBackend(Library("user", 0)), "TARGETKEY")
# Serialize result only if needed; reconcile writes no mapping file itself.
```

This callable performs authorized Zotero writes when needed. For isolation,
inject a backend exposing library, collection, target_items, library_items,
get_item, can_add, add, import_entry, and a verified metadata lookup callable;
use checks=3 and delay=0. Normalize sources first to validate occurrence IDs.
For recovery across calls, supply `journal_path="import-attempts.json"`.
Ordinary CLI imports enable this automatically without requiring mapping export.
Keep the same per-target journal even if source files/row positions change.
Source IDs and opaque caller locations are independent of article attempt IDs.

Optional catalog resolver:

```python
from pathlib import Path
from ingest import lookup_metadata
from reference_metadata import MetadataCatalog

resolver = MetadataCatalog(Path("verified-records.ris"), lookup_metadata)
result = reconcile(refs, backend, "TARGETKEY", lookup=resolver)
```

Catalog misses use the original resolver; contradictory or multiple catalog
candidates remain pending. RIS is decoded strictly as UTF-8 then GB18030 unless
`encoding` is specified. JSON uses UTF-8 with optional BOM.

No zotero-output, EndNote parser, Word dynamic-field generator or merger is
implemented by this interface.

## Optional Built-In Citation Bridge

The independent uncited-prose mode uses this same mapping version and existing
import coordinator; see [citation-workflow.md](citation-workflow.md). Its optional
top-level `citation_bundle_id` binds to a reviewed evidence snapshot. Ordinary
imports do not add it or require it. Source-location data retains all citation
occurrences per source reference, without collapsing distinct documents.

The separately consented note interface adds `zotero-input.provenance-notes/1`
jobs and `zotero-input.note-results/1` results; see
[provenance-notes.md](provenance-notes.md). Notes use final adopted item identities
and never change the ordinary mapping schema or launch Zotero-output.
