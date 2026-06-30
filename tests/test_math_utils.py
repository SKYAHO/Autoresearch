import unittest

from autoresearch import math_utils
from autoresearch.math_utils import add


class AddTests(unittest.TestCase):
    def test_adds_numbers(self):
        self.assertEqual(add(2, 3), 5)
        self.assertEqual(add(-1, 1), 0)
        self.assertEqual(add(1.5, 2.25), 3.75)


class WeightedAverageTests(unittest.TestCase):
    def test_calculates_weighted_average(self):
        result = math_utils.weighted_average([80, 90, 100], [1, 2, 1])

        self.assertEqual(result, 90)

    def test_rejects_empty_values(self):
        with self.assertRaisesRegex(ValueError, "at least one value"):
            math_utils.weighted_average([], [])

    def test_rejects_mismatched_lengths(self):
        with self.assertRaisesRegex(ValueError, "same length"):
            math_utils.weighted_average([80, 90], [1])

    def test_rejects_zero_total_weight(self):
        with self.assertRaisesRegex(ValueError, "total weight"):
            math_utils.weighted_average([80, 90], [0, 0])


if __name__ == "__main__":
    unittest.main()
