import argparse
from pathlib import Path
import unittest
from unittest.mock import patch

import zotero as z


def item(key, doi=None, pmid=None, title=None):
    return {"key": key, "data": {"key": key, "itemType": "journalArticle",
                               "DOI": doi, "extra": f"PMID: {pmid}" if pmid else "",
                               "title": title or key, "collections": ["TARGET"]}}


class CollectionImportTests(unittest.TestCase):
    def plan(self, refs, target, *, sync=False, resolve=True):
        args = argparse.Namespace(source="refs.xlsx", collection_key="TARGET",
                                  no_resolve_pmid_doi=not resolve)
        with patch.object(z, "extract_source_references", return_value=refs), \
             patch.object(z, "collection_items", return_value=target), \
             patch.object(z, "_paginated_get", side_effect=AssertionError("full library queried")):
            return z.collection_import_plan(args, include_prune=sync)

    def test_empty_target_does_not_check_library_or_resolve_pmids(self):
        refs = [{"pmid": str(10000000 + n), "title": f"Study {n}"} for n in range(37)]
        with patch.object(z, "resolve_reference_pmid_doi", side_effect=AssertionError("unneeded lookup")):
            plan = self.plan(refs, [])
        self.assertEqual(plan["counts"]["import_new"], 37)
        self.assertEqual(plan["counts"]["already_in_target"], 0)
        self.assertNotIn("add_existing_to_collection", plan)

    def test_only_target_membership_skips(self):
        refs = [{"doi": "10.1000/in", "title": "In target"},
                {"doi": "10.1000/elsewhere", "title": "Elsewhere"}]
        plan = self.plan(refs, [item("IN", doi="10.1000/in")])
        self.assertEqual([entry["source"]["doi"] for entry in plan["import_new"]],
                         ["10.1000/elsewhere"])
        self.assertEqual(plan["counts"]["already_in_target"], 1)

    def test_pmid_to_doi_resolution_only_when_needed(self):
        refs = [{"pmid": "12345678", "title": "Study A"}]
        with patch.object(z, "resolve_reference_pmid_doi",
                          return_value=({"pmid": "12345678", "doi": "10.1000/a", "title": "Study A"}, True)) as lookup:
            plan = self.plan(refs, [item("A", doi="10.1000/a")], sync=True)
        lookup.assert_called_once()
        self.assertEqual(plan["counts"]["already_in_target"], 1)
        self.assertEqual(plan["counts"]["prune_candidates"], 0)

    def test_target_pmid_match_needs_no_network(self):
        refs = [{"pmid": "12345678", "title": "Study A"}]
        with patch.object(z, "resolve_reference_pmid_doi", side_effect=AssertionError("unneeded lookup")):
            plan = self.plan(refs, [item("A", pmid="12345678")])
        self.assertEqual(plan["counts"]["already_in_target"], 1)

    def test_same_title_with_conflicting_doi_is_not_skipped(self):
        refs = [{"doi": "10.1000/new", "title": "Same title"}]
        plan = self.plan(refs, [item("OLD", doi="10.1000/old", title="Same title")], resolve=False)
        self.assertEqual(plan["counts"]["import_new"], 1)

    def test_no_pmid_lookup_if_target_has_no_doi_only_items(self):
        refs = [{"pmid": "12345678", "title": "Another study"}]
        with patch.object(z, "resolve_reference_pmid_doi", side_effect=AssertionError("unneeded lookup")):
            plan = self.plan(refs, [item("A", pmid="87654321")])
        self.assertEqual(plan["counts"]["import_new"], 1)


if __name__ == "__main__":
    unittest.main()
