import unittest

from seomyeon.models import validate_source_id


class SourceIdTests(unittest.TestCase):
    def test_source_id_matches_citation_grammar(self) -> None:
        self.assertEqual(validate_source_id("FACT_1-a"), "FACT_1-a")
        for invalid in ("", "한글1", "contains space", "a" * 65):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_source_id(invalid)
