import io
import unittest
from unittest.mock import patch
import zotero as z


class MetadataTests(unittest.TestCase):
    def test_pubmed_structured_abstract_and_doi_fallback(self):
        xml = b'''<PubmedArticleSet><PubmedArticle><MedlineCitation>
        <PMID>12345678</PMID><Article><ArticleTitle>Example</ArticleTitle>
        <Abstract><AbstractText Label="METHODS">Test <i>with</i> markup.</AbstractText>
        <AbstractText Label="RESULTS">A &amp; B.</AbstractText></Abstract></Article>
        </MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="doi">
        10.1000/example</ArticleId></ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>'''
        with patch.object(z.urllib.request, 'urlopen', return_value=io.BytesIO(xml)):
            entry = z._pubmed_fetch('12345678')
        self.assertEqual(entry['abstract'], 'METHODS: Test with markup.\n\nRESULTS: A & B.')
        self.assertEqual(entry['doi'].strip(), '10.1000/example')
        ris = z._pubmed_to_ris(entry)
        self.assertIn('AB  - METHODS: Test with markup. RESULTS: A & B.', ris)
        self.assertIn('M2  - PMID: 12345678', ris)
        self.assertNotIn('N2  - PMID', ris)

    def test_crossref_abstract_markup(self):
        entry = z._crossref_item_to_entry({'abstract': '<jats:p>A <i>test</i>.</jats:p><jats:p>Next.</jats:p>'})
        self.assertEqual(entry['abstract'], 'A test. Next.')
        self.assertIn('AB  - A test. Next.', z._doi_to_full_ris(entry))

    def test_missing_abstract_is_not_fabricated(self):
        self.assertNotIn('AB  -', z._pubmed_to_ris({'pmid': '12345678'}))

    def test_clean_import_preserves_metadata_and_removes_children(self):
        ris = ('TY  - JOUR\nTI  - Example\nN2  - PMID: 12345678\n'
               'AB  - Real abstract\ncontinued text\nM2  - Existing extra\n'
               'N1  - note\nnote continuation\nRN  - another note\nC7  - custom identifier\n'
               'L1  - https://example.org/paper.pdf\nL2  - https://example.org/page\n'
               'UR  - https://example.org/article\nER  -\n')
        clean = z.clean_ris_for_import(ris)
        self.assertIn('M2  - PMID: 12345678', clean)
        self.assertIn('AB  - Real abstract\ncontinued text', clean)
        self.assertIn('M2  - Existing extra', clean)
        self.assertIn('M2  - RIS C7: custom identifier', clean)
        self.assertIn('UR  - https://example.org/article', clean)
        for excluded in ['N1  -', 'RN  -', 'L1  -', 'L2  -', 'note continuation', 'N2  - PMID']:
            self.assertNotIn(excluded, clean)
        self.assertEqual(z.clean_ris_for_import(clean), clean)

    def test_incomplete_records_rejected(self):
        with self.assertRaises(ValueError):
            z.clean_ris_for_import('TY  - JOUR\nTI  - incomplete')

    def test_import_command_uses_cleaner(self):
        args = z.argparse.Namespace(yes=True, file=None, text='TY  - JOUR\nN1  - note\nER  -', session='test')
        with patch.object(z, 'connector_post', return_value=z.Response(201, {}, '')) as post:
            with patch.object(z, 'dump_json'):
                z.cmd_import_records(args, 'RIS')
        self.assertNotIn('N1', post.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
