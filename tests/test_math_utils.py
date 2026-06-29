import unittest

from autoresearch.math_utils import add


class AddTests(unittest.TestCase):
    def test_adds_numbers(self):
        self.assertEqual(add(2, 3), 5)
        self.assertEqual(add(-1, 1), 0)
        self.assertEqual(add(1.5, 2.25), 3.75)


if __name__ == "__main__":
    unittest.main()
