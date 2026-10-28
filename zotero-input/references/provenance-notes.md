# Opt-In Citation Provenance Notes

## Consent and Evidence

This feature is built into zotero-input, not an installed-skill dependency.
Ordinary imports still create no child notes or attachments. Import consent does
not authorize this mode. Before source-location work or notes, explain:

"I can add a note under each adopted Zotero reference recording the document,
original sentence, supporting source quotation, and verified section/page. This
usually costs considerably more tokens because it involves source and location
checking. Would you like me to do this?"

Record a clear yes to both this optional operation and its disclosed cost; the
two CLI flags represent that single informed decision. No separate second prompt
is required when the user has already explicitly given informed approval.
On refusal, deliver citations/import results without notes or deep note work.

Reuse reviewed cached abstracts/excerpts. Read only selected sections/pages of
already chosen references where necessary, not all candidate papers. An abstract
quote is identified as abstract evidence, not full text. Page labels and printed
page numbers are different. No access or reliable locator -> explicitly missing;
never fabricate them. If precise full-text provenance was requested and only an
abstract is accessible, explain the partial result before treating it as complete.
Do not copy entire articles/chapters into notes. Respect short-excerpt limits.

Optional selected-page extraction with existing `pdftotext` or installed
`pypdf`/`PyPDF2` (no automatic installation):

```bash
python citation_handoff.py evidence-pages --pdf paper.pdf --page 3 --page 4 --approve-notes --acknowledge-token-cost --out pages.json
```

At most five pages per call; file indexes are 1-based. It does not infer printed
labels or chapter names. Scan/OCR and unavailable reader errors remain explicit;
accessible HTML/text or supplied extracted pages are alternatives without a new
plugin/package. Add the actual units to the review, verify quotes/locators, then
regenerate the bundle. If references are already imported use `map-existing`,
not another import (see citation-workflow.md).

## Build a Plan

```bash
python citation_handoff.py notes-plan --bundle cited/citation-bundle.json --mapping imported.json --approve-notes --acknowledge-token-cost --outdir notes
```

Emits `notes-job.json` (`zotero-input.provenance-notes/1`),
`zotero-add-provenance-notes.js` and escaped `notes-preview.html`. Inspect previews
locally, not by loading whole evidence caches into model context. Plans do not
write to Zotero. An unmapped/unverified parent remains unresolved; no guessed
parent or cross-library matching by item key.

One note per document snapshot + adopted item aggregates its citation usages.
Separate documents retain separate notes, even with the same local reference
number. Each note contains document ID/name/hash, snapshot time, original sentence
and offset, citation marker, source quotation, support assessment, source URL/file
and hash, evidence kind, section, PDF file page index and printed page label.
Missing information is explicitly unavailable. Notes are snapshots, not tracking
or promises that later document edits will update them.

Only final verified adopted items are used. Fallback imports therefore get their
own notes, not notes on the old item that never entered the destination.
Existing notes are not edited/deleted; explicit matching snapshots are reused.
The note marker and full normalized semantic content must both match, under the correct
regular parent and selected library. Changed/duplicate markers stop another write.
The generated snapshot header's valid recorded timestamp is excluded from this
comparison; the saved note keeps its original first-recorded time. Rebuilding
the same reviewed snapshot later does not create a new note or overwrite it.
Document text, quotes, locators and assessments remain part of the comparison.
Native DOM parsing and Python HTML parsing preserve inline words and normalize
block boundaries/entities consistently. Timestamp changes in quotations are not
ignored. Corrupt journals and concurrent writers stop before creating notes.

## Optional Plugin Route: Actual Operation

Read-only runtime discovery:

```bash
python citation_handoff.py notes-capabilities
```

The bundled MCP adapter uses the same initialize/tools-list/library-identity
protocol as write-paths.md, checking runtime schemas, not tool names alone.
The inspected desktop extension's actual operation is `write_note`, not an
assumed `create_note`. Its create call is:

```json
{
  "name": "write_note",
  "arguments": {
    "libraryID": 1,
    "action": "create",
    "parentKey": "ADOPTED_ITEM_KEY",
    "content": "<p>Escaped provenance snapshot HTML</p>"
  }
}
```

`libraryID` is resolved internal identity, not the public group/user ID. `parentKey`
is mandatory for this feature, even if optional in the server schema. Create
requires `action` + `content`; updates/append use `noteKey` but are **not** used
here. No tags, PDFs, attachments or standalone notes are requested. The inspected
handler creates `Zotero.Item('note')`, sets `libraryID`, resolves the regular
parent in that library, sets `parentKey` and HTML via `setNote`, then `saveTx`.
Its response's `noteKey` or success flag does not verify actual content.

On an advertised compatible writer, run:

```bash
python citation_handoff.py notes-write --job notes/notes-job.json --result-out notes/write-result.json
python citation_handoff.py notes-verify --job notes/notes-job.json --result-out notes/final-verification.json
```

`--plugin-url` allows the existing supported loopback MCP address. Do not install
or require the extension. `notes-write` performs one create attempt per note,
bounded reads (default three) and a final verification. A lost response with an
actual matching child is success, not grounds for a second note. Results use
`zotero-input.note-results/1` and report outcome, actual `note_key`, parent/library,
verified flag and failure reason. `notes-verify` never discovers/calls writers.

CLI writes a durable `<notes-job>.journal.json` by default; `--journal` can specify
its path. Preserve it. Started/unknown prior writes block repeated creates even
across invocations. Do not run the same job concurrently or delete/change its
journal to retry. After uncertain results use read-only verification and report
pending. Explicit permissions errors are failure; **never switch channels to
bypass a denial**. Writer unavailable before any attempt permits the native route.

## Local Markdown Snapshot

`notes-plan` also writes `citation-provenance.md` before any Zotero note attempt.
It retains one entry per document/citation/support occurrence, even when several
entries adopt the same item. It records the original sentence and offsets,
document title/ID/path/hash when supplied, reference identity, verbatim quotation,
cached source-unit context, source URL/path/hash, section, physical PDF page and
printed page label separately, plus adopted library/item/collection identity and
membership/fallback status. Missing information stays explicit. Original reference
text is distinguished from a generated bibliography description.

The same export is available independently, including before import:

```bash
python citation_handoff.py provenance-export --bundle cited/citation-bundle.json --out citation-provenance.md
python citation_handoff.py provenance-export --bundle cited/citation-bundle.json --mapping imported.json --out citation-provenance-linked.md
```

This request authorizes only a local file, not Zotero child notes or additional
full-text searches. Generation reads the reviewed JSON already on disk, uses no
network and does not feed all evidence back into the model. First evidence
verification is still required. Abstract-only context remains explicitly abstract;
no unavailable full text or page is inferred. Additional deep reading still needs
the separate cost warning and consent. Existing files are never overwritten.
The document is an immutable-at-creation snapshot, not a live synchronization
of manuscript edits. Zotero member verification shown is the mapping's prior
snapshot, not a fresh check at export; the MD does not certify note creation.
The format is versioned `zotero-input.provenance-markdown/1`.

## Plugin-Free Native Route

The whole native implementation is shipped as `scripts/zotero_notes.js` and
embedded with escaped data into the generated JS. It uses Zotero's supported
data API, not SQLite, an invented HTTP endpoint or an external skill.

1. Confirm there was no denied or uncertain write in this job. If there was,
   use read-only verification, not another writer. Choose this route only when
   no compatible plugin write was attempted.
2. In Zotero Desktop open **Tools > Developer > Run JavaScript**, enable
   **async** mode and execute the generated `zotero-add-provenance-notes.js`.
   Available authorized desktop automation can do this. Without automation,
   the user can run this supplied operation; do not claim it ran automatically.
3. The script resolves public personal/group identity to internal libraryID,
   validates all regular parents, source metadata and collection membership,
   checks matching child markers/content, then creates only missing child notes
   with `new Zotero.Item('note')`, `libraryID`, `parentKey`, `setNote` and `saveTx`.
   Library editability is mandatory. No existing note is replaced.
4. It returns JSON after checking actual child content/parent/library/membership.
   Save the returned result as evidence if execution is available, then always
   run `notes-verify` above against the built-in read-only local API.
   Report successful notes only after this independent final verification.

The native script checkpoints attempted note IDs in the intended Zotero
profile's `zoteroInput.provenanceNoteAttempts` preference before `saveTx` and
rechecks saved children on every execution, including after restart. Invalid
preference contents stop writes. This is a local attempt guard, not successful
write evidence. Preserve operation history; uncertain attempts require read-only
checks and must not switch to another writer or clear the guard to retry.

If Run JavaScript or desktop automation is unavailable/restricted, the preview
can guide a user to select the verified parent, use Notes > Add (or Add Note),
and enter the child note through Zotero's normal UI. This is not automatic success;
perform the same read-only verification. If no authorized execution route exists,
return the complete plan and mark the write pending. Core citation creation and
ordinary Connector imports remain usable without this optional automation.

## Evidence and Verification Limits

Zotero's official [JavaScript API](https://www.zotero.org/support/dev/client_coding/javascript_api)
documents the native developer console, async mode, item APIs and saveTx;
[Notes](https://www.zotero.org/support/notes) documents child notes and their UI.
Historical local extension source contained `write_note`; the disabled isolated
profile's 2026-10-03 `tools/list` did not expose it. An authorized fresh isolated
profile on 2026-10-04 with write support enabled did advertise a compatible
schema. Its single consented create attempt timed out, and later read-only
checks found no matching child. The attempt journal remains uncertain; no
second create or alternate writer was attempted. That historical job remains
unverified and must not be replayed. Native-console/UI execution is unverified
because desktop automation was unavailable in the test environment.

On 2026-10-14 the user explicitly authorized real-paper tests in their experiment
collection. Current runtime discovery exposed a compatible `write_note` schema.
The ordinary coordinator imported the PubMed-verified AlphaFold reference
(PMID 34265844, DOI 10.1038/s41586-021-03819-2) into the verified selected target.
One create call produced a child note; an independent backend with writer
discovery disabled verified content, parent identity and membership. Repeating
the same job reused the same note key, without a second create. This confirms
that plugin route in the tested environment, not the native/no-plugin route or
the old uncertain job. The Markdown captured abstract evidence and explicitly
missing PDF/printed pages; it did not invent full-text locators.

Source presence, schema availability and a sent request are not actual-write
verification. The source path is evidence, not a runtime dependency. Python
request/state tests, executed JS API simulations and real browser DOM tests
cover comparison/idempotence boundaries, not real Zotero editor persistence.
Never test on unrelated user items or clear the uncertain journal to force retry.
