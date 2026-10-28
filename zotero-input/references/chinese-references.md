# Chinese References

Read this when Chinese extraction or metadata needs more than the ordinary
DOI/PMID lookup. Existing collection selection, reuse, fallback, permission and
membership-verification rules remain unchanged. Do not request an account or
extra package just for Chinese imports.

## Extraction and Matching

- Tables recognize title aliases such as 题名、标题、文献标题、论文题目;
  作者、第一作者、首作者; 年份、出版年、发表年; 编号、序号.
- TXT/Markdown/CSV/RIS use strict UTF-8 (optional BOM), then GB18030. Supply
  `--encoding utf-16` or another known encoding when necessary; malformed input
  is reported, not replaced with corrupted characters. XLSX/DOCX use XML decoding.
- Clearly marked citations such as `张三,李四. 糖尿病防治[J]. 中华医学杂志,2024.`
  yield title, first author and year. Unmarked or wrapped references may need
  agent-assisted parsing; do not infer arbitrary author/title boundaries.
- Preserve short titles and unresolved raw references. A short Chinese title
  without DOI/PMID needs matching author or year to reuse a candidate. Same-title
  multiple candidates or contradictory IDs/authors/years remain pending.
- Compare Chinese full names or explicitly split names; do not guess Chinese
  surnames, transliterations or English translations. Bilingual titles are usable
  only when the same verified source explicitly provides both.

## Verification Routes

1. Match the target collection first, then the selected library. An unambiguous
   existing item is reused without extra external lookup. Do not overwrite it.
2. For missing/new/fallback records, prefer DOI, then PMID. If these fail to
   verify a Chinese reference, search its exact title with author/year in an
   accessible journal/publisher page or Chinese database. CNKI entry points
   include https://www.cnki.net/ and https://oversea.cnki.net/; they are candidate
   sources, not guaranteed APIs or guaranteed accessible pages. Other lawful
   accessible sources or user-supplied platform exports are equally usable.
3. Inspect the actual article record, not a search snippet, home page or generated
   summary. Record its URL, identifiers, displayed title(s), authors and year;
   retain available journal/volume/issue/pages and the real abstract. Compare
   all supplied identifiers, author and year before importing.
4. When a retrieved page includes explicit `citation_*` metadata, use the HTML
   extractor below. Otherwise obtain a platform RIS export, or build the small
   verified JSON record below from fields actually shown by the source. Do not
   assume CNKI uses those tags or offers an undocumented endpoint.
5. Access denial, CAPTCHA, unavailable pages or incomplete records are not
   authorization to bypass controls. Use another accessible source or report the
   unresolved reference and request an export only if necessary. Metadata missing
   title/authors or conflicting with the reference cannot be imported. Missing
   abstract is reported; never substitute PMID, page description or invented text.

Large abstracts stay in local metadata files, not model messages. No full-text
download, child note, snapshot or attachment is required for this workflow.

## Optional Supplement

Commands are relative to the skill's scripts directory; use the real absolute
path and `python -X utf8 -B` in tools.

```bash
# Ordinary import remains unchanged
python zotero.py import-references --source refs.xlsx --collection TARGETKEY --yes

# Supplement the original source; its original IDs/text are retained
python zotero.py import-references --source refs.docx --metadata-catalog verified-records.ris --collection TARGETKEY --yes
python zotero.py import-references --source refs.csv --metadata-catalog verified-records.json --collection TARGETKEY --yes

# Or use a platform RIS export as the source itself
python zotero.py import-references --source exported.ris --collection TARGETKEY --yes
```

RIS types JOUR/BOOK/THES/CONF/CHAP/RPRT/GEN are supported. The reader preserves
title, authors, identifiers, journal, year, volume/issue/pages, URL and actual
abstract where provided. It is not a complete importer for every RIS field.
Note/attachment tags are not copied. Legacy PMID-only `AB`/`N2` is recognized as
an identifier and reported as a missing abstract.

JSON catalogs have `{"records": [record, ...]}`. Each record may contain:

```json
{
  "title": "糖尿病防治",
  "first_author": "张三",
  "year": "2024",
  "metadata": {
    "title": "糖尿病防治",
    "creators": [{"lastName": "张三", "firstName": ""}],
    "year": "2024",
    "journal": "示例期刊",
    "doi": null,
    "pmid": null,
    "abstract": null,
    "url": "https://example.org/article/123",
    "title_aliases": []
  },
  "metadata_provenance": {
    "kind": "publisher",
    "url": "https://example.org/article/123"
  }
}
```

This is a schema example, not verified literature. Replace values with the actual
source record. `kind` supports publisher/database with an HTTP(S) record URL,
source_export with `format: RIS` and a file path, or user_verified with an explicit
`description` of user verification. The script validates required fields and
identity consistency, but does not fetch or authenticate the claimed source URL.
The agent/caller must actually verify that source. Title and creators are required;
other fields are null/omitted when unavailable. Preserve complete Chinese names
as `lastName` with empty `firstName` when the source does not split them.

Catalog matches must be unique and consistent. A miss uses original lookup; an
ambiguous/conflicting catalog match stays pending. The optional metadata and
provenance fields can also appear directly in each structured reference from
`--references-json` or the Python API. They do not enable mapping export by default.

## Retrieved HTML Helper

This command reads an already retrieved article HTML file; it does not browse,
log in, write to Zotero or depend on a plugin:

```powershell
python -X utf8 -B zotero.py metadata-from-html --file article.html --url "https://actual-source/article" --kind database | Out-File -Encoding utf8 verified-records.json
```

It requires explicit `citation_title` and `citation_author` tags and retains
available `citation_doi`, `citation_pmid`, `citation_publication_date`,
`citation_date`, `citation_journal_title`, `citation_volume`, `citation_issue`,
`citation_firstpage`, `citation_lastpage` and `citation_abstract`. Generic page
`title` or `description` is not article metadata. A file without the required
tags fails clearly; use the other routes instead. Validate the generated JSON
before import, especially when a shell creates an empty file on command failure.

## Verification Boundary

The regression suite covers Chinese extraction, matching, supplied metadata,
exports, mappings and simulated collection writes. It does not establish live
CNKI availability, real Zotero/Connector writes or a universal Chinese database
scraper. These require separately authorized integration checks in the actual
environment. PDF text extraction still requires an available `pdftotext` or OCR.
