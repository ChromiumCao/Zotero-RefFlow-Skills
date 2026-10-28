---
name: zotero-output
metadata:
  version: "0.1.1"
description: >-
  Merge manuscripts or repair and reorder citations within one document,
  deduplicate references, and generate editable DOCX with native Zotero fields
  or Markdown citation source. In any language, activate directly for merging
  papers or texts explicitly containing references, or reorganizing citations.
  For generic text-merging requests, first lightly inspect the source content;
  activate only when references or citation fields are present. Supports DOCX,
  TXT, Markdown and PDF. Runs independently with read-only Zotero library
  mapping, including explicit no-collection mode; zotero-input is optional for
  missing items/imports. Also supports explicitly static numbered output without
  Zotero. Plain text merging without references and standalone Zotero imports
  are outside this skill.
---

# Zotero Output

Build a new editable manuscript with the original citation-to-reference
relationships intact. Source files remain recoverable. A DOI hyperlink, a
number rendered as text, and a native Zotero citation field are different things.
Only claim a Word output is fully verified after Microsoft Word + Zotero Refresh
and a library-link check succeed. The portable writer can produce native fields
without Word installed, but that output remains pending integration validation.
Installation of Zotero Input is not required. Dynamic output needs reliable,
freshly verified Zotero identities; embedded numeric IDs and hyperlinks alone
are not proof. Explicitly static output has no live fields or Word Refresh claim.

Perform reference/plan review yourself when the evidence is clear. Review flags
are audit markers, not a mandatory user approval flow. Ask only for materially
missing style/order/identity information while continuing independent work.
Use `skill-creator` for modifications to this skill, document tooling for Word
layout, and available PDF/OCR tooling for PDF reconstruction. Keep ordinary
`zotero-input` imports independent; never add output requirements to that skill.

## Semantic activation

Match the request's meaning in any language, not a fixed keyword list. These
English examples are a routing blueprint; equivalent requests in Chinese or
other languages follow the same decisions.

| Request meaning | Routing decision |
| --- | --- |
| "Merge these texts." | Before invoking the full workflow, lightly inspect source content for references. Activate if present; otherwise continue ordinary text merging. |
| "Merge these two papers." | Activate directly for the manuscript/reference merge. |
| "These two passages both have references. Merge them and reorganize the references." | Activate directly. |
| "This one file has two passages with inconsistent references. Reorder them." | Activate directly; multiple files are not required. |
| "Import these references into Zotero." | Use zotero-input alone unless document reconstruction is also requested. |

The lightweight check reads available source text, especially the document end,
and readily accessible citation-field markers. Evidence includes a bibliography,
identifiable bibliographic entries, or Zotero/EndNote/Mendeley citation fields.
Numbers, equations, footnote marks or hyperlinks alone are insufficient. Do not
import, create a collection or perform the full citation scan merely to decide
whether a generic merge needs this skill. Inconclusive extraction is not proof
that references are absent; continue normal source reading until the distinction
is clear. An explicit request to use this skill activates it, but never invent
references or Zotero writes when the supplied content has none.

## Workflow

1. At the start, if the user has not already chosen an existing collection or
   requested a new one, ask once whether to use an existing Zotero collection.
   State that a new collection is the default if none is specified or no reply
   arrives within 60 seconds. Continue read-only source inspection while waiting;
   resolve the destination before writes. Honor explicit choices without asking
   again. See [workflow.md](references/workflow.md) for timeout and late replies.
   Exception: an explicit request for no collection skips this question, creation
   and timeout default entirely. Do not infer this choice from missing Input or
   missing collection membership. The original 60-second default stays unchanged.
   Resolve source paths and merge order (use supplied order by default), target
   Zotero library/collection, and one CSL style. If only a reference list is
   requested, produce that without inventing body citation positions. Dynamic
   bibliography-only mode must explicitly retain uncited items in bibliography
   metadata, rather than add hidden body citations. See [workflow.md](references/workflow.md).
   Use a unique temporary working directory outside the final result folder for
   scan/mapping/plan files, working copies, previews and task-specific scripts.
2. Scan before editing: `python -X utf8 -B scripts/zotero_output.py scan --source
   a.docx --source b.md --out scan.json --references-out references.json`.
   The scan records source hashes, paragraph/story positions, fields, bibliography
   entries and candidate plain-text markers. Inspect `issues`; ambiguous mappings
   require correction in scan/plan, never invented replacement references.
   Add `--reference-list` only for actual list-only sources. After scan corrections,
   run `references --scan scan.json --out references.json` to regenerate the input.
3. First resolve existing library items without Input: `... resolve --scan scan.json
   --no-collection --out mapping.json`, only when no collection was explicitly
   chosen; otherwise pass the resolved `--collection KEY`. This path performs
   read-only native-URI/DOI/PMID/exact-title matching and reports unresolved or
   ambiguous items. It never imports, creates collections or adds membership.
   If imports/membership changes are needed, optionally use `zotero-input`, passing
   the generated
   `references.json` to `import-references --references-json ... --collection ...
   --yes --mapping-out mapping.json`. Read that skill's current contract. Its
   library reuse and fallback policies are authoritative. Do not modify it, assume
   its mappings are complete, or import solely to force a duplicated item identity.
   If Input is absent, offer installation/use only for that enhancement, or let
   the user organize/import in Zotero and rerun read-only resolution. Never create
   a staging collection behind an explicit no-collection request. For ordinary
   non-dynamic output use `plan --static --style source-text`; this source-based
   numbered format requires neither Input nor Zotero. Other static CSL formats
   need an available renderer; do not claim source-text implements them.
4. Build a citation plan: `... plan --scan scan.json --mapping mapping.json --out
   plan.json --style vancouver`. Review plain-text candidates and source bibliography
   ranges, then set `reviewed: true` on accepted citations and bibliography ranges.
   Structured native Zotero fields have explicit identities; imported foreign
   fields must be intentionally relinked using the verified destination mapping.
   Duplicate references across source files may adopt one canonical destination
   item only with compatible verified identifiers/metadata. Record aliases rather
   than deleting items from Zotero. See [interfaces.md](references/interfaces.md).
5. Fetch destination records read-only: `... items --plan plan.json --out items.json`.
   Require parent bibliographic items and authoritative item URI. Collection mode
   also requires actual membership; explicit library scope verifies library
   identity without requiring collection membership.
   A local `user/0` alias is not a global user identity. Resolve an offline local
   user URI from an authoritative Zotero export when available; never synthesize
   it from an internal library number. See [word-fields.md](references/word-fields.md).
6. Build a new copy: `... build --plan plan.json --items items.json --out merged.docx`.
   DOCX packages are patched before composition; rich multi-DOCX composition needs
   `docxcompose` (see `doctor`) or an existing trusted document merge workflow.
   Review master layout using `composition_reviewed:true`; the helper rejects
   source-specific headers/footers, comments, embedded objects and multi-source
   endnotes needing another merge route. Missing capability is a blocker, not permission to discard images, tables,
   footnotes or fields. TXT/MD/PDF create a reconstructed editable document; PDF
   layout is not losslessly recoverable. Markdown output uses `--out merged.md`.
7. `... verify --document merged.docx --plan plan.json --items items.json` proves
   structural counts, item bindings and source expectations. Then open the copy
   specifically in Microsoft Word and use Zotero Document Preferences/Refresh.
   A helper is provided: `scripts/refresh_word.ps1 -Document <absolute DOCX>`.
   Run on an output copy only. Resolve dialogs with available authorized UI tools;
   do not kill Word/Zotero or bypass macro security. Verify again after refresh,
   check library links (UI or proven native item resolution), and render/inspect
   the final pages with document tooling. The refresh helper's receipt and
   structural report represent different checks; do not turn their false/unknown
   integration fields into true without actual evidence.
8. Deliver a clean result folder: normally the final DOCX, optionally one short
   Markdown explanation. Preserve real authors/editors and conservatively clear
   explicit automation credits, including bare Codex/GPT6/model names in author
   fields (not people whose names merely contain these words). Run
   `... clean-metadata --document merged.docx` after the final Word save/close,
   then verify again; the refresh helper already performs this cleanup. Do not
   add generated-by credits to remarks, comments, headers/footers or the optional
   explanation. Preserve human reviews, revisions, academic disclosures,
   cited-paper authors and Zotero fields/preferences. Multiple DOCX author strings
   are combined in source order; differing source last editors are left unset
   rather than inventing a final editor. Single-source identities are inherited.
   Publish only after validation, confirm the published
   copy matches the verified document, then remove this run's temporary files.
   JSON manifests and refresh receipts are working artifacts, not required for
   Zotero's later Word updates. Do not include previews, logs, raw library data,
   temporary scripts or repaired source copies unless requested. Preserve actual
   rendering dependencies when Markdown is the requested main output. Follow
   [workflow.md](references/workflow.md#delivery-and-cleanup) for cleanup scope.

## Decisions that matter

- Preserve all source reference occurrences, including duplicate bibliography
  entries and repeated citations. Identity is `(document_id, reference_id)`, not
  a printed number. Every new citation location receives a unique citationID;
  a multi-item citation receives one field.
- A collection organizes library records; changing its members does not edit
  a manuscript. Later edits use Zotero Add/Edit Citation and Refresh. Hand-typed
  numbers or bibliography text are not tracked additions.
- Preserve citation options (locator, label, prefix, suffix, suppress-author,
  unsorted); store real note context. Never flatten foreign or malformed fields
  and silently call them converted. Unknown EndNote/Mendeley field variants,
  cross-paragraph citation fields, ambiguous author-date markers, scanned PDFs and unsupported
  package features require an explicit repair/alternate route. Partial plans are
  review artifacts, not completed merged documents.
- Reject stale source hashes, overlapping spans, changed anchors, missing mappings,
  unverified membership, malformed item URIs, conflicting DOI/PMID or incompatible
  metadata. Working manifests retain hashes, source aliases and verification
  status until acceptance; ordinary delivery does not retain these sidecars.
  Never modify the user's existing library metadata or
  delete existing records just to deduplicate an output.
- Use explicit style selection; CSL governs dynamic ordering and numbering. Cached
  visible numbers are provisional until Zotero refreshes. Markdown is editable
  citation source, not a Word-like live integration; regenerate when it changes.

## Development

`python -X utf8 -B scripts/zotero_output.py doctor` reports local dependencies.
`python -X utf8 -B -m unittest discover -s scripts -p 'test_*.py' -v` runs isolated
fixtures. Real library tests are read-only unless the user explicitly authorizes
isolated new test records. Report offline and actual Word integration separately.
See [word-fields.md](references/word-fields.md) for dependencies and field semantics,
and [verification.md](references/verification.md) for actual tests and boundaries.
