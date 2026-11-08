# Bundled Search Provenance

`scripts/_vendor/nature_citation.py` is a modified copy of the `nature-citation`
search and export script from Yuan1z0825 and the nature-skills contributors.
The original was taken from an installed copy (manifest version 2.1.0) on
2026-10-03. The recorded hash matches the pinned upstream file with CRLF line
endings. The upstream repository and this distribution use Apache-2.0.
[Upstream source at commit 66668ae](https://github.com/Yuan1z0825/nature-skills/blob/66668aea4f8cbdfd6791bbeac5aed53f233eac88/skills/nature-citation/scripts/nature_citation.py)
· [Upstream license at that commit](https://github.com/Yuan1z0825/nature-skills/blob/66668aea4f8cbdfd6791bbeac5aed53f233eac88/LICENSE)
· [Distributed license](../LICENSE) · [Attribution](../NOTICE)

Original script SHA-256:
`E145269FB98326C985AE95A8DE2B29009592A9A16B9C1763BB1E6C550E3604FB`.

Zotero Input makes unrestricted `all` the default journal scope and retains the
explicit scopes. XML parsing uses defusedxml when available and a bounded
standard-library parser rejecting entities, DOCTYPE and NUL otherwise.
The report and browser labels describe unreviewed candidates. The HTML browser
escapes embedded JSON, validates links and renders selection text as text.
The contact User-Agent is built from `ZOTERO_REF_EMAIL` when that variable is
set and otherwise carries only the tool name, so no placeholder address ships.
Legacy format/author tests are included. Search remains metadata candidate
generation; approved citation rendering is in `citation_handoff.py`.

The built-in search carries complete creator metadata locally and exports
reference/segment JSON, RIS/ENW/RDF, TSV and review artifacts. The citation draft
bridge imports full local records without putting full abstracts in repeated
agent messages. The citation coordinator delegates imports to the existing
`ingest.py`; no second import engine or external skill resolution is used.

Public Crossref and PubMed are supported discovery routes; the existing verified
Chinese-source metadata supplement is retained for publishers/CNKI/Wanfang and
other accessible sources. There is no new paid API or mandatory account.
