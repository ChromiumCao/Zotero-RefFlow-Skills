# Workflow and review decisions

This skill reconstructs existing citation relationships. It does not add new
scientific support, rewrite claims, or require reading cited papers' full text.
The agent performs the review flags itself when source evidence is sufficient;
flags are not a requirement to ask the user to approve every reference.

## Execution modes (v0.1.1)

Output installs and runs independently. Start with read-only `resolve` for native
Zotero URIs or existing-library DOI/PMID/exact-title matches. No Input module is
imported. Dynamic output still requires a reachable Zotero local API and fresh
identity/CSL verification; original local numeric IDs are not portable identity.
An unknown/ambiguous reference remains pending and blocks dynamic build.

For explicitly non-dynamic work, `plan --static --style source-text` uses reviewed
source entries and produces plain numbered DOCX or Markdown, without Input,
Zotero API calls, native fields or a Word Refresh claim. It retains source entry
text, or readable embedded bibliographic metadata, and numbers by first citation.
This basic format does not implement APA, GB/T or another CSL style. Use a proven
available renderer for other static styles, otherwise report that limitation.
Do not silently switch a request for live citations to static output.

The collection preference below is unchanged. Static formatting itself performs
no collection operations; if a collection was requested/defaulted, report any
unavailable creation/organization capability separately, without pretending it
was completed. An explicit no-collection choice avoids that prerequisite.

## Resolve the request

Use the source order given by the user and DOCX as the default deliverable.
If file order is unspecified, use the provided order and state that assumption.
Use the requested CSL style. If absent, infer an explicit journal style from
context or ask one short style question while scanning; numeric Vancouver is a
reasonable stated fallback when the user only asks for unified numbering.
Do not infer a specific Chinese national-standard edition from "Chinese style".
Confirm an unfamiliar style slug against the official Zotero/CSL style repository
or an existing local CSL file before final refresh.

### Destination preference at the start

**Explicit exception:** when the user says not to use a collection, skip the
question, creation and 60-second default. Use `--no-collection`. This is a library
verification scope, not permission to create a hidden temporary collection.
For all unspecified choices keep the original rule below. The pure
`library_mapping.collection_decision` helper can represent ask/wait/new/existing/
none decisions; it neither runs an interactive timer nor creates collections.

When this skill is activated for actual document work, ask early whether to use
an existing Zotero collection unless the request or established session context
already specifies one for this task or explicitly requests a new collection.
Do not treat a prior run's default collection or the current UI selection as the
user's choice. Do not ask again for a destination already chosen. Offer a concise
question in the user's language:

> Would you like to use an existing Zotero collection? If so, provide its name
> and library (or collection key). Otherwise I will create a new collection. If
> no destination is specified or no reply arrives within 60 seconds, I will use
> that default while continuing to inspect the sources.

Use asynchronous input when available, with "Create a new collection (default)"
and "Use an existing collection; specify its name/key" as suggested choices.
An explicit choice to create new, or a reply giving no existing destination,
allows the default immediately. For an unanswered question, allow 60 seconds
from when it was presented, continuing independent read-only source work. A
preselected suggestion is not a submitted reply. After that interval, state the
default and create a fresh collection for the already authorized merge/import.
This is a destination preference, not an approval request.

If the user chooses an existing collection but its identity is missing or
ambiguous, resolve its exact library, hierarchy and key before writing. Silence
on that required clarification does not cancel the user's existing-collection
choice or permit substituting a new destination. A late reply before any write
can change the destination. If a collection was already created or populated,
report that state and the late preference before redirecting work; do not
silently delete collections or move/remove earlier memberships.

A fresh collection can be called `Zotero Output <source stem> <date>` in the
chosen library; absent an explicit library choice, use the personal library.
Ensure the new destination is distinct rather than silently adopting an older
collection with the same name. Use a time/run suffix when needed. Do not create
a collection for a plan-only request or a merge found to contain no references.

Only for the optional import/collection-writer collaboration, read the current
`zotero-input` skill. Resolve its installed directory from the
skill catalog or a known project copy; never embed the developer's Windows paths
in generated reusable commands. Missing Input limits this enhancement, not
read-only mapping or static generation. Do not replace its reuse/fallback contract.

Create/select a requested/defaulted collection through optional Input's supported writer or available authorized
Zotero UI. Resolve the actual library and collection key before import. No
reference import, metadata modification or deletion is authorized by a request
to inspect the skill or prepare a plan only.

## Scan and repair

```text
python -X utf8 -B <output-skill>/scripts/zotero_output.py scan --source <a.docx> --source <b.md> --out <scan.json> --references-out <references.json>
```

For files containing only reference lists, add `--reference-list`. This applies
to every source in that scan. For mixed list/manuscript requests, scan sources
separately and combine their documents arrays in the same scan schema.

Review the paragraphs, reference entries, candidate spans and bibliography
removal ranges. `scan` is deliberately conservative:

- A bibliography heading initially marks all remaining main paragraphs as
  bibliography. Narrow the range if an appendix, acknowledgments or more prose
  follows. Join wrapped entries or split incorrectly combined entries.
- Without a heading or native bibliography field, an unlabeled manuscript
  bibliography needs an explicit reviewed range. `--reference-list` is for
  actual lists, not a shortcut for discarding manuscript prose.
- Numeric brackets and numeric superscripts are candidates. Equations, units,
  powers and author affiliations require exclusion with a reason.
- Parenthesized author-date clusters are heuristically matched by first surname
  and year. Narrative `Smith (2020)`, footnote-only reference systems, Pandoc
  `[@key]` without a visible reference list and unusual markers need agent-assisted
  extraction. Add exact spans and source reference IDs; do not guess.
- DOCX uses `word/document.xml`, including table paragraphs, plus footnotes and
  endnotes. Paragraph indexes refer to all paragraphs in each XML part, not
  only direct body children. Text offsets count visible `w:t`, tabs and breaks;
  field instruction text is excluded. Existing citations are protected from
  duplicate numeric detection.
- Zotero/Mendeley CSL fields and EndNote fields with embedded `<EndNote><Cite>
  <record>` metadata are supported. Other variants need an explicit parser or
  reviewed source metadata. Foreign library URIs are provenance and must not
  be asserted as identities in the destination library.
- PDF extraction uses PyMuPDF or `pdftotext`. Scans require available OCR first.
  Inspect reading order, columns, ligatures, wrapped reference entries, missing
  superscripts and page boundaries. PDF output is an editable reconstruction;
  the helper preserves extracted text, not the original PDF layout or images.
  Add figures/tables through a document workflow if they are required.

After scan corrections regenerate the import file:

```text
python -X utf8 -B <output-skill>/scripts/zotero_output.py references --scan <scan.json> --out <references.json>
```

Source files remain untouched. Correcting a scan/plan cannot bypass the source
SHA-256 check. If a source changed, rescan and rebuild its mappings.

## Import and plan

Prefer this independent read-only route when identity can be established:

```text
python -X utf8 -B <output-skill>/scripts/zotero_output.py resolve --scan <scan.json> --no-collection --out <mapping.json>
python -X utf8 -B <output-skill>/scripts/zotero_output.py plan --scan <scan.json> --mapping <mapping.json> --style vancouver --out <plan.json>
```

Use `--no-collection` only for that explicit preference. Otherwise replace it with
`--collection <KEY>` after resolving the original destination policy. Add public
`--library-type group --library-id <ID>` when applicable. Collection resolution
is read-only and reports nonmembers as pending; it cannot create/add/import.

Optional Input can resolve/import library misses and add verified memberships
under its unchanged rules. When absent, explain the unresolved references and
offer Input collaboration or user-managed Zotero import/organization followed by
another `resolve`. Input's current collection requirement is not bypassed: for
an explicit no-collection request, user-managed import or an explicitly changed
destination preference is needed before invoking its collection-based importer.
No automatic collection creation or arbitrary first-candidate adoption is allowed.

The established Input collaboration route remains:

```text
python -X utf8 -B <input-skill>/scripts/zotero.py import-references --references-json <references.json> --collection <KEY> --yes --mapping-out <mapping.json>
python -X utf8 -B <output-skill>/scripts/zotero_output.py plan --scan <scan.json> --mapping <mapping.json> --style vancouver --out <plan.json>
```

For a group destination pass `zotero-input`'s public group-library parameters.
Only verified success mappings are downstream bindings. A library-wide match
alone is insufficient. Keep every source occurrence, including duplicates.
`zotero-input` may produce a fallback duplicate when reuse is unavailable;
output deduplication adopts one compatible verified destination item and records
aliases. It does not merge, move, edit or delete library records.

Resolve each citation against its own source bibliography. In a plan set
`reviewed:true` on accepted citations and removal ranges, then top-level
`reviewed:true`. Set `issues_reviewed:true` on a source after its scan issues
are handled. A non-citation candidate can use `action:"ignore"` plus `reason`;
native citation fields cannot be ignored in a relinking output. For an ambiguous
candidate repair its `references`, `item_keys` and `issue` against verified
source bindings. Unresolvable identity is a reason to ask for clarification,
not to select the first search result.

The input helper may enrich metadata without filling its original `identifiers`
snapshot. The output script checks readable contradictions and fetches final CSL
metadata from Zotero before build. Inspect anomalies reported by `zotero-input`.

## Build and final acceptance

```text
python -X utf8 -B <output-skill>/scripts/zotero_output.py items --plan <plan.json> --out <items.json>
python -X utf8 -B <output-skill>/scripts/zotero_output.py build --plan <plan.json> --items <items.json> --out <merged.docx>
python -X utf8 -B <output-skill>/scripts/zotero_output.py verify --document <merged.docx> --plan <plan.json> --items <items.json> --out <verification.json>
```

`items` uses only the local read-only API. It checks current library identity and
collection membership, fetches CSL JSON and resolves an authoritative URI.
In explicit no-collection mode it verifies library identity without membership.
All outputs use new filenames; existing output/sidecar files are rejected.
Run the staged commands in a unique temporary workspace outside the final result
folder. The sample JSON files and `merged.docx` are working artifacts until final
acceptance, not a list of files to hand to the user. Keep generated manifests
beside the working DOCX for the refresh helper's preflight checks.

One DOCX retains its package while citation runs are patched. A mixed merge
with one DOCX retains that DOCX package and inserts text sources in supplied
order. Multiple rich DOCX files require `docxcompose` and a review of master
layout (`composition_reviewed:true`). The composer is unsuitable for differing
source headers/footers, endnotes, comments and embedded objects. The helper
rejects these cases rather than discarding them. Use an available trusted
section-preserving merge workflow and the final field/prose audits instead.
Tracked changes, textboxes, altChunk, cross-paragraph citations and unsafe
replacement spans require repair on a separate copy.

Open the generated copy specifically in Microsoft Word. Set Zotero Document
Preferences to the chosen style/locale and Refresh. On Windows the optional
`scripts/refresh_word.ps1` calls the installed Zotero macro and waits for hydrated
fields, saves and closes only its generated document. It leaves failures open
for inspection and does not suppress dialogs or alter macro security. Actual
host GUI permissions still apply. Do not force-kill Word/Zotero.

Verify again after refresh, resolve any orphan/relink prompts, and confirm Zotero
library linkage through its citation UI or equivalent evidence that hydrated
fields resolve to actual local numeric item IDs and their authoritative URIs.
Render and inspect all final pages using available document tooling. Microsoft
Word's own PDF export can serve as an alternate renderer when the bundled
renderer is unavailable; retain an explicit record of that substitution.

Cached field results contain the original marker and provisional reference
titles. They are intentionally not presented as a final formatted bibliography.
Final style, sorting, numbering and disambiguation come from Zotero Refresh.
Report structural verification, real refresh and library resolution separately.
Report source-list items with no body citation (`uncited_source_items` in the
manifest). The default manuscript bibliography follows actual citations. Set
`preserve_uncited:true` only when uncited entries should intentionally remain;
otherwise explain their exclusion instead of silently treating all imported
collection members as the document's bibliography.

## Delivery and cleanup

For the default Word request, leave only the final merged DOCX in the result
folder, optionally accompanied by one concise Markdown explanation. That
explanation can state source order, citation style, target collection, reference
counts, completed checks, material limitations and how to edit citations later.
Do not retain a long technical test log as the ordinary explanation. PDF previews,
page images, JSON mappings/plans/item snapshots, build manifests, refresh receipts,
downloaded styles, temporary scripts and prepared source copies are temporary
unless the user explicitly requests them.

Complete structural, source, library-link and actual Word/layout verification
before cleanup. Copy the verified DOCX to a new final filename and compare its
hash with the verified working copy before removing working artifacts. Native
Zotero citation fields and preferences are inside the DOCX; Zotero Add/Edit
Citation and Refresh do not need the external JSON files. The bundled
`refresh_word.ps1` is a build-time validation helper requiring a working manifest;
after delivery, normal updates use Zotero's Word controls.

Before publishing, preserve real document authors, editors, company and manager
information and remove only clearly identified automation-credit strings
such as "generated by python-docx" or "generated by Codex". The writer cleans
new DOCX packages automatically. Bare Codex, GPT6/GPT-6 and recognized model
names in author/last-editor fields are non-author signatures and are removed;
unknown identities and ordinary mentions of AI are preserved. Standalone credit
lines can be removed while retaining surrounding metadata notes. Single-DOCX
processing inherits its real author/editor. Multiple DOCX author strings are
deduplicated in source order and joined with semicolons; an agreed last editor
is retained, conflicting last editors are left unset. No name is inferred from
filenames, accounts or citation authors. Word can write metadata when saving,
so repeat the conservative check after the last Word save/close (including any later save used
for layout edits or previews) and before final verification and hashing:

```text
python -X utf8 -B <output-skill>/scripts/zotero_output.py clean-metadata --document <merged.docx>
```

The refresh helper includes this step and accepts `-Python <executable>` when a
specific Python runtime is needed. Human comment/revision authors and records
are preserved. Only clearly identified agent/model author attributes can be
blanked; comment text, prose, academic disclosures, citation
itemData author lists and Zotero custom preferences. It does not add authors or
generated-by credits to Markdown frontmatter or delivery explanations. Never
add an automation credit in remarks, comments, headers/footers or body text.
This controls the delivered version; a later independent save in Word may write
its account name again; a real user/editor name is retained under this policy.

For static output, use the same reviewed plan/build/verify stages:

```text
python -X utf8 -B <output-skill>/scripts/zotero_output.py plan --scan <scan.json> --static --style source-text --out <plan.json>
python -X utf8 -B <output-skill>/scripts/zotero_output.py build --plan <reviewed-plan.json> --out <merged.docx>
python -X utf8 -B <output-skill>/scripts/zotero_output.py verify --plan <reviewed-plan.json> --document <merged.docx>
```

No `--items` is needed in static mode. `--out merged.md` creates plain numbered
Markdown without a CSL sidecar. All review/hash/conflict checks still apply.

For cleanup requested after delivery, preserve the user's current DOCX even if
its hash or citation count differs from the tested version. Do not regenerate,
revert or refresh their edits just to match old receipts. Check that cleanup
leaves the current document unchanged, and scope prior verification claims to
the version actually tested.

Clean only files created for this run. On Windows, resolve each deletion target
and verify that it lies within the exact task workspace/result directory before
deleting; do not follow links or use a broad extension-based purge. Preserve
source files, unrelated existing files, reusable skill scripts, installed styles
and Zotero library contents. Remove temporary files rather than moving them to
another hidden result subfolder or archive. A failed or paused run may keep its
working checkpoint outside the result folder for resumption; do not delete the
evidence still needed to resolve unfinished verification. Preserve debugging
artifacts after successful delivery only if the user requests them.

If editable Markdown citation source is the requested main deliverable, retain
the minimal files that it actually depends on, such as its referenced CSL JSON
bibliography and any local CSL style. These are functional dependencies, not
debug output. Its separate URI manifest and other verification files are still
temporary unless requested. A Markdown explanation accompanying DOCX has no such
sidecar requirement.

## Reference-list-only and Markdown

Use `plan --mode bibliography` for a list-only request. DOCX stores selected URIs
in bibliography `uncited` metadata without inventing hidden body citations.
This structure is tested offline; automatic update in a document with zero body
citations is not promised. Zotero may require a real citation before processing
its bibliography. If that gate appears, explain it and use an explicitly chosen
editable formatted-list route; do not insert fake or hidden citations.

`build --out merged.md` produces editable Pandoc citation source, CSL JSON and
a URI/key manifest. It references the official CSL repository; cache the style
locally if offline rendering is required. Pandoc `--citeproc` creates formatted
static output. For Word live fields from Markdown, use the documented Better
BibTeX `zotero.lua` route or build DOCX directly with this helper.
DOCX-to-Markdown is text reconstruction and requires an explicit reviewed
conversion for tables/media; notes require a separate Markdown footnote route.

Later Word changes use Zotero Add/Edit Citation and Refresh. Hand-typed numbers
and manually edited bibliography text are not tracked additions. Collection
membership edits do not change the document's citation set.
