"""Quote matching folds typographic quotes and whitespace, nothing else."""

import unittest

from tools.eval_bridge.instantiation import _canon_quote, _quote_matches


class QuoteNormalisationTests(unittest.TestCase):

    def test_typographic_quotes_still_match(self):
        source = '12:50 销售协调员：我拟对外回复：“五批都从一厂仓出，发运又都在11月。”'
        model = '12:50 销售协调员：我拟对外回复："五批都从一厂仓出，发运又都在11月。"'
        self.assertTrue(_quote_matches(model, source))

    def test_wrapped_whitespace_still_matches(self):
        self.assertTrue(_quote_matches('LOT-028|SZ2|2026-11-03', 'LOT-028|SZ2|\n2026-11-03'))

    def test_paraphrase_is_still_rejected(self):
        source = '12:50 销售协调员：我拟对外回复：“五批都从一厂仓出。”'
        self.assertFalse(_quote_matches('五批都从二厂仓出', source))

    def test_empty_quote_is_rejected(self):
        self.assertFalse(_quote_matches('', 'anything at all'))

    def test_folding_touches_only_quotes_and_whitespace(self):
        self.assertEqual(_canon_quote('“a  b”'), '"ab"')
        self.assertEqual(_canon_quote('甲 乙'), '甲乙')


if __name__ == '__main__':
    unittest.main()