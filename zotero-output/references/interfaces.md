# Versioned local interfaces

Optional Input collaboration uses `zotero-input.references/1` and
`zotero-input.mapping/1`. Read its contract when using that route; do not change
it. Output also supplies the independent mapping adapter below. Schemas are
local JSON, UTF-8, with missing optional
metadata represented by null/absence. No workflow starts from opening a JSON file.

## Scan: zotero-output.scan/1

Top level `documents` preserves merge order. Each document has `document_id`
(absolute source file URI), absolute `path`, `sha256`, `format`, `paragraphs`,
`references`, `citations`, `bibliography_ranges`, `issues`, `issues_reviewed`.

Paragraph: `part`, zero-based `paragraph`, `text`, `note_index`, `fields`,
`superscripts`. For TXT/MD/PDF, `part` is `text` and paragraph indexes are extracted
line indexes. For DOCX indexes are in the indicated XML part.

Reference: document-local `reference_id`, original `raw_text`, optional `label`,
DOI/PMID/title/first_author/year, optional source location and native CSL metadata.
Native `foreign_uri` is provenance, not a destination lookup constraint.

Citation: document-local `citation_id`, `kind`, `part`, `paragraph`, zero-based
`start`/exclusive `end` in visible text, exact original `text`, `note_index`,
`references` array (`reference_id`, `options`), optional original `field`,
`issue`, `reviewed`. Options retain locator/label/prefix/suffix/suppress-author,
author-only/position where present, and cluster `properties.unsorted`.

Bibliography range: `part`, `start` paragraph index, exclusive `end`, `reviewed`.
Ranges remove whole paragraphs, so narrow them around actual bibliography text.
Do not remove unrelated figures, notes, section properties or prose.

Run `references` after editing scan metadata. Generated `source_location`
preserves the bibliography locator, foreign URI and all candidate citation IDs
as opaque caller data. The input skill does not extract body positions.

## Independent mapping: zotero-output.mapping/1

`resolve` emits occurrence rows compatible with `plan`, with explicit `scope`
(`collection` or `library`), `collection_policy` (`collection` or `none`),
`target_collection`, `complete`, `mappings` and `writes:0`. Rows preserve source
IDs, identifiers, adopted item/URI and match evidence. In library scope,
`library_verified:true`, `target_collection:null`, `membership_verified:false`
and `outcome:library_existing` mean an existing parent was freshly read in the
selected library, without asserting membership. Collection rows instead require
actual membership and `outcome:target_existing`. Pending rows cannot build.

An Input mapping cannot be downgraded to library scope by deleting its target
or flipping a flag. Only an explicit Output no-collection mapping activates that
scope. Native URIs must agree with fresh authoritative exports, or independent
compatible bibliographic evidence must identify the destination. Ambiguity,
source/CSL contradictions and unavailable identity remain pending.

## Plan: zotero-output.plan/1

Top level: `style` (CSL slug), `locale`, `mode` (`manuscript`/`bibliography`),
`documents`, canonical `items`, `source_bindings`, `item_aliases`, `mapping_issues`,
`preserve_uncited`, `reviewed`. Optional `composition_reviewed` and
`markdown_reconstruction_reviewed` acknowledge reviewed conversion choices.

Each canonical item has `key`, `item` (library + item_key + optional URI),
`identifiers`, `target_collection`. Its internal key is
`<library type>:<public ID or originating local alias>:<item key>`, not a BibTeX
key. The input's `id_kind` remains attached to the library identity.
`scope` defaults to `collection` for backward compatibility. Library scope needs
`collection_policy:"none"` and null targets; it never disables identity checks.
`citation_mode:"static"` is explicitly selected by `plan --static`; its internal
`static:*` keys identify source entries, never Zotero items/URIs. Static mode
supports `style:source-text`, records readable entries and first-citation numbers,
and requires no item bundle. Named CSL styles are rejected on this basic route.

`source_bindings` keys are `document_id + "\n" + reference_id`. Every accepted
source occurrence has its own entry, even when many map to one canonical item.
`item_aliases` records output adoption; no library deletion/merge is implied.
Canonicalization needs shared DOI/PMID or exact title+author+year and no readable
contradictions, within the same library. Conflicting records cannot be collapsed.

Citations add `item_keys` aligned with `references`, `action` (`replace`/`ignore`)
and optional ignore `reason`. Keys must agree with verified `source_bindings`.
Repeated identical item/options in one cluster collapse; different locators for
the same item remain distinct. Repeated locations become separate fields.

Review flags document agent audit, not user authorization. Missing mappings,
membership failure, changed source hashes, identity conflict, overlapping spans
and incomplete item bundles cannot be overridden by flags.

## Item bundle: zotero-output.items/1

`records` entries contain canonical `key`, full `item` identity, authoritative
`uri`, CSL `csl`, `membership_verified` and `target_collection`. Independent library
rows additionally carry `scope:library` and `library_verified:true`, and do not
assert collection membership. `items` builds
this from fresh local reads without importing or editing records. A manually
provided/offline bundle must carry equivalent verified provenance; an invented
true flag is not evidence. The agent should fetch anew before final acceptance.

User/0 is a local API alias. CSL export `id` can supply a valid
`http://zotero.org/users/local/<LOCAL_USER_KEY>/items/<KEY>` URI. A positive public
ID in the exact item's API envelope can supply its user/group URI. Never use a
plugin-internal library number, display name, another document's namespace or
another machine's user/0 to fabricate an item URI.

## Output manifest and verification

DOCX `<output>.manifest.json` records source hashes, output hash, field counts,
unique citation IDs, adopted URIs, source prose verification and pending Word
integration state in the temporary workspace. `verify` structurally checks
ZIP/XML/relationships, native
fields, cluster identities/multiplicity, bibliography membership, chosen style
and source prose with citation positions. It does not assert Word/UI success.

`refresh_word.ps1` separately creates `<output>.word-refresh.json` after actual
macro return, field hydration, save/close and output hashing. It leaves
`library_links_verified:false`; the agent must check actual item resolution.
Its preflight hash applies to a newly built copy. For subsequent user edits,
refresh interactively or prepare a newly verified manifest; do not falsify hashes.

DOCX verification reports `metadata_clean:true` when recognized automation
credits/model signatures are absent; real author/editor information is allowed.
The writer checks properties before build
verification; `refresh_word.ps1` repeats cleanup after Word saves/closes and before
recording the final hash. `clean-metadata --document <DOCX>` explicitly updates
the named DOCX in place and returns its new hash and changed XML part names. It
preserves Zotero preferences, citation authors and substantive text, and does
not rewrite an already clean file. It does not create another delivery sidecar.
Unknown author names, real company/manager details, disclosures, human comments
and revisions are retained. Bare known model names are removed from author
fields, without erasing legitimate names that merely contain those words.

Static DOCX verification reports `citation_mode:static`, zero native citation/
bibliography fields and no Word-refresh/library-link success. It validates text,
numbers, bibliography and metadata. Static Markdown similarly contains plain
numbered text; dynamic Markdown remains Pandoc citation source as before.

These DOCX sidecars are validation artifacts, not runtime dependencies of the
finished document. After acceptance publish only the DOCX and optional concise
Markdown explanation, then remove the run's working artifacts. See
[delivery and cleanup](workflow.md#delivery-and-cleanup). The final DOCX continues
to work with Zotero's normal Word controls after its external JSON files are gone.

Markdown produces `<stem>.references.json` (CSL array), Pandoc-compatible citation
keys in `<output>.md.manifest.json`, and explicit URI mapping. These keys are
stable for the adopted library/key, while numbered presentation depends on
the chosen style and current document order. They are not Word live fields.
Retain the referenced CSL bibliography when delivering Markdown as citation
source; the separate URI manifest can be removed after validation.

## Python entry points

Add this skill's `scripts` directory to `sys.path` in an isolated caller:

```python
from scan_sources import scan, references_input
from citation_plan import make_plan, fetch_items
from word_output import build_docx, verify_docx
from library_mapping import resolve_library

scanned = scan(paths)
input_payload = references_input(scanned)
mapping = resolve_library(scanned, no_collection=True)  # explicit user choice
# Alternatively obtain mapping from authorized optional Zotero Input collaboration.
plan = make_plan(scanned, mapping, "vancouver")
# Agent reviews source relationships and plan flags here.
items = fetch_items(plan)
report = build_docx(plan, items, output_path)
```

Errors raise `OutputError`; CLI errors return exit 2 and `completed:false`.
All library operations in these modules are read-only. Only the separately
invoked input skill performs requested import/collection writes.
