# Bundled Search Provenance

`scripts/_vendor/nature_citation.py` is a local copy of the search/export script
from the user's installed `nature-citation` skill (manifest version: 2.1.0),
read on 2026-10-03. The original installation was not modified. Attribution is
kept here rather than requiring that skill at runtime. No upstream license is
asserted when one was not present with the copied source.

Original script SHA-256:
`E145269FB98326C985AE95A8DE2B29009592A9A16B9C1763BB1E6C550E3604FB`.

Local changes: unrestricted `all` journal scope is the default, other explicit
scopes remain supported; XML parsing uses an existing defusedxml when available,
or a bounded standard-library parser rejecting entities/DOCTYPE/NUL when absent;
generic report/browser labels no longer imply a default CNS-only search.
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
