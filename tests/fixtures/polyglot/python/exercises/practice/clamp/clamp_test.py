import unittest

from clamp import clamp


class ClampTest(unittest.TestCase):
    def test_inside(self):
        self.assertEqual(clamp(5, 0, 10), 5)

    def test_below(self):
        self.assertEqual(clamp(-1, 0, 10), 0)

    def test_above(self):
        self.assertEqual(clamp(11, 0, 10), 10)

    def test_at_low(self):
        self.assertEqual(clamp(0, 0, 10), 0)

    def test_at_high(self):
        self.assertEqual(clamp(10, 0, 10), 10)

    def test_bad_range(self):
        with self.assertRaises(ValueError):
            clamp(1, 2, 1)
