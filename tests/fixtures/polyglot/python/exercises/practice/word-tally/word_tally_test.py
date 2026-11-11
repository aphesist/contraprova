# Synthetic fixture for contraprova's harness tests.

import unittest

from word_tally import tally


class WordTallyTest(unittest.TestCase):
    def setUp(self):
        self.empty = {}

    def assertTally(self, sentence, expected):
        self.assertEqual(tally(sentence), expected)

    def test_empty(self):
        self.assertTally("", self.empty)

    def test_one_word(self):
        self.assertTally("hi", {"hi": 1})

    def test_repeated_word(self):
        self.assertTally("hi hi", {"hi": 2})

    def test_case_folding(self):
        self.assertTally("Hi hI", {"hi": 2})

    @unittest.skipIf(False, "never skipped")
    def test_two_words(self):
        self.assertTally("a b", {"a": 1, "b": 1})

    def test_extra_spaces(self):
        self.assertTally("  a   a ", {"a": 2})

    def test_tabs_and_newlines(self):
        self.assertTally("a\tb\na", {"a": 2, "b": 1})


if __name__ == "__main__":
    unittest.main()
