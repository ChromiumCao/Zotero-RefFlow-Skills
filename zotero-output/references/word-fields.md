# Native Word field semantics and dependencies

## Runtime requirements

Python 3.10+ runs the helpers. Text scanning and mapping use the standard library.
DOCX XML operations require `lxml`; reconstructed DOCX and composition require
`python-docx`. Multiple rich DOCX sources additionally require `docxcompose`.
PDF text extraction uses available PyMuPDF or `pdftotext`; OCR is a separate
available capability. `requirements.txt` describes Word helper dependencies;
PyMuPDF is optional. Resolve the host's available document runtime first.
Do not install global packages or substitute an unrelated application silently.

Microsoft Word with Zotero's Word plugin is required for final Word refresh.
Zotero Input is optional: the read-only resolver works with Zotero Desktop's local
API alone. Explicit no-collection mode verifies library identity instead of
membership. Static source-text output needs neither Zotero nor Input, creates no
native fields and makes no CSL/Refresh claim. Its numerical source keys are not
Zotero identities. Document properties retain real authors and conservatively
remove only known automation credits/model signatures after the final save.
The optional PowerShell helper is Windows-only. Native Word is not required to
scan, plan or produce structurally valid fields. Markdown rendering needs an
available Pandoc/citeproc workflow; the helper itself does not run it.

## Citation fields

Instruction:

```text
ADDIN ZOTERO_ITEM CSL_CITATION <JSON>
```

The writer emits begin, instruction, separator, visible result and end runs.
Instruction text has `xml:space="preserve"`. JSON contains a unique `citationID`
for the insertion location, `properties.noteIndex`, `citationItems` with each
selected item URI and embedded CSL `itemData`, and the CSL citation schema URI.
The same item can occur in many fields; one multi-item citation is one field.
Local item IDs are not guessed: URI-valued IDs are resolved by Zotero on Refresh.
Real library resolution hydrates fields with actual numeric item IDs.

Locator/label, prefixes/suffixes, author suppression, author-only and existing
cluster unsorted state are retained. The writer does not manufacture
`formattedCitation` or `plainCitation`; Zotero computes those cached values.
The original visible marker is provisional until refresh.

Main-text `noteIndex` is zero. After composing documents, footnote/endnote field
context is recomputed from actual note references in the resulting document,
not copied from a source's potentially renumbered note ID. Unknown note styles
still require Document Preferences and real integration validation.

## Bibliography and preferences

Instruction:

```text
ADDIN ZOTERO_BIBL {"uncited":[],"omitted":[],"custom":[]} CSL_BIBLIOGRAPHY
```

`uncited` contains arrays of authoritative item URIs when reference-list-only
or preserve-uncited mode is explicitly selected. Do not insert hidden citations
as a workaround. Zero-body-citation Word processing is an unverified boundary;
the helper's automatic refresh receipt requires at least one actual citation.

Zotero preferences are Word custom properties `ZOTERO_PREF_1`, `_2`, etc. They
store a chunked XML data-version 3 record, session ID, style/locale and Field
preferences. The writer registers `docProps/custom.xml` in the OPC content types
and root relationships, retaining unrelated custom properties. Zotero writes
its actual application version and updated preferences during Refresh.

Normal bibliography fields can span paragraphs. The story scanner supports
those and preserves unrelated multi-paragraph fields such as TOCs. It rejects
cross-paragraph *citation* fields requiring source repair. Tables, relationships,
media and numbering are preserved by package patching or supported composition;
replacement through an unsafe drawing/bookmark/field wrapper is rejected.

## Integration evidence and primary references

Field serialization, URI resolution and bibliography metadata were checked
against [Zotero integration source](https://github.com/zotero/zotero/blob/main/chrome/content/zotero/xpcom/integration.js).
Operation guidance follows [Zotero Word plugin documentation](https://www.zotero.org/support/word_processor_plugin_usage).
The [community DOCX field notes](https://github.com/drguptavivek/zotero-use/blob/main/references/word-docx-citations.md)
provide useful namespace/field examples; their separate approval policy is not
imported into this skill. [Better BibTeX's Pandoc guide](https://retorque.re/zotero-better-bibtex/exporting/pandoc/)
documents the optional Markdown-to-live-Word route.

The included testing record distinguishes structural, Word refresh and actual
renumbering checks. No source citation software is assumed compatible merely
because its visible text resembles a Zotero reference.
