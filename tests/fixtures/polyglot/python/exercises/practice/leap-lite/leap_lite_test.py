import unittest

from leap_lite import is_leap


class LeapTest(unittest.TestCase):
    def test_2000(self):
        self.assertTrue(is_leap(2000))

    def test_1900(self):
        self.assertFalse(is_leap(1900))

    def test_2024(self):
        self.assertTrue(is_leap(2024))
