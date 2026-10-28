# Verification record

Validated on Windows, Python 3.13, Microsoft Word 16.0 and the running local
Zotero library, 2026-10-02 to 2026-10-03 (Asia/Shanghai). This record describes
the tested environment; it is not a promise about all Zotero/Word versions.

## Automated fixtures

The v0.1.0 snapshot was checked again on 2026-10-15 (Asia/Shanghai): all 23
isolated tests passed, including the composer, PDF and current zotero-input
normalization contract. The runner used a writable task-local temporary directory
after the sandbox's default temporary directory denied file writes. No live
Word refresh or Zotero write was repeated for this version snapshot; the actual
integration evidence below remains the earlier dated test.

Run:

```text
python -X utf8 -B -m unittest discover -s <skill>/scripts -p "test_*.py" -v
```

Twenty-three tests passed in the complete suite, including the optional rich
composer and PDF branches. Coverage includes:

- colliding source reference numbers and cross-document deduplication;
- numeric ranges, repeated citation items and separate location IDs;
- author-date candidates, superscripts and heading-free reference lists;
- DOI identifiers in external DOCX hyperlink targets, even when not visible;
- native Zotero field relinking with options and embedded EndNote records;
- bibliography fields spanning multiple paragraphs;
- footnote package preservation and actual note context;
- bold/italic runs, tables, images and multi-DOCX composition;
- mixed source order preserving the rich package, and rejection of differing
  source headers before any output is published;
- PDF-to-editable reconstruction and Markdown CSL sidecars;
- the installed input skill accepting generated structured references and
  retaining repeated source positions;
- unverified/missing mappings, conflicting identifiers, invalid item URIs,
  stale sources, source overwrite, unresolved candidates and changed prose.

Tests use isolated temporary files and mocked mappings. They perform no live
Zotero imports, collection writes or metadata changes. The input-contract test
only calls source normalization. Optional dependencies absent in another runtime
are reported as skipped tests, not successful integrations.

## Actual local integration

Read-only Zotero API checks confirmed that an offline personal-library CSL
export supplies the exact local-user item URI. The fixture used existing parent
items already in their existing collections. No library records, metadata,
collection memberships, attachments or notes were modified.

The generated DOCX opened in actual Microsoft Word and its installed
`ZoteroRefresh` macro returned. Fields acquired formatted/plain citation caches
and actual numeric Zotero item IDs, retaining their authoritative local URIs.
Word generated a Vancouver citation and complete bibliography. Structural,
cluster, source-prose and citation-position checks passed again after refresh.
One page exported by Word was rendered to PNG and inspected; no clipping,
overlap, missing glyphs or layout defects were observed.

The packaged document renderer was attempted but lacked `pdf2image` in this
environment. Word's native PDF export plus PyMuPDF page rendering was used
instead. No rendering dependency or alternative office application was installed.

Actual automatic renumbering was checked in a second disposable document:

| Operation | Citation results |
| --- | --- |
| Two existing items initially cited | `(1)`, `(2)` |
| Add the second item's native citation at document start, then Refresh | `(1)`, `(2)`, `(1)` |
| Delete that added citation, then Refresh | `(1)`, `(2)` |

This tests actual Zotero document numbering after document edits. It does not
claim the unavailable interactive Add/Edit Citation dialog was driven in this
environment. Normal user additions through that dialog follow Zotero's own
documented workflow.

The first refresh helper attempt successfully hydrated fields but could not
hash a DOCX while Word locked it. The helper was corrected to save and close
before hashing, then passed on the two-item fixture.

## Metadata cleanup checks (2026-10-24)

Historical policy: the blanket identity removal tested in this section is
superseded by v0.1.1's conservative preservation rules below.

The expanded isolated suite contains 29 tests. Added checks cover default
python-docx remarks, inherited authors and last editors, namespace variants,
custom author/credit properties, comment authors and a simulated Word save that
restores ownership metadata. Cleanup preserves citation author lists and Zotero
preferences and is idempotent. A CLI check on a disposable copy of the previously
merged document retained its eight native citation fields; document XML and
Zotero preferences stayed byte-identical and the user's original was untouched.
The PowerShell refresh helper now calls metadata cleanup after save/close and
before hashing; its syntax is checked, but this metadata change has not repeated
the earlier actual Word/Zotero Refresh integration test.

## v0.1.1 isolated validation (2026-10-28)

All **53 isolated tests passed** for this snapshot. The current suite preserves
the existing Input mapping/collection tests and adds
independent native-URI and existing-library mapping with Input imports blocked,
unfiled item reuse, explicit no-collection routing, unchanged ask/59.999/60-second
policy boundaries, pending/ambiguous/conflicting identities and foreign local URI
rejection. Mocked API reads verify library scope and collection scope separately;
the Output adapter has no write operations. Static DOCX/MD and bibliography-only
output use the existing plan/build/verify pipeline, with no live fields, no API
reads and explicit source-text formatting limits. Metadata checks preserve real
authors/editors/company/reviews/disclosures, remove known banners and bare
Codex/GPT6 signatures, combine source authors explicitly and check repeated
cleanup without changing citation data.

These are isolated simulations, not live Word/Zotero or interactive timer tests.
For v0.1.1, actual Word Refresh/renumbering, group-library integration, default
collection creation/UI selection and asynchronous 60-second timing are **pending
integration verification**. The helper's deterministic routing boundary does not
prove elapsed UI timing or successful creation. Earlier dated live evidence
above is retained as history, not claimed as a new v0.1.1 integration run.
An actual loopback API probe was attempted outside the sandbox restriction and
received connection refused (WinError 10061) at port 23119. Zotero was therefore
not available for a new live integration run; no library or collection writes
were attempted.

## Boundaries

Group-library integration, every foreign field variant, complex note styles,
zero-body-citation bibliography-only refresh, OCR and all possible rich document
layouts have not been live-tested. Differing headers/footers, comments, embedded
objects and multi-source endnotes are rejected by the bundled rich composer;
they require an alternate trusted workflow. PDF layout/media reconstruction
and Markdown rendering have separate review requirements.

Keep private local integration fixtures and quota-monitor state outside the
distributed skill. This skill's bundle contains no copied Zotero library data,
account configuration, machine-specific paths or runtime logs.
