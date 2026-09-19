import unittest
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
class TestCases(unittest.TestCase):
    def tearDown(self):
        plt.close("all")
    def test_cumsum_correctness(self):
        data = {"A": [1, 2, 3], "B": [4, 5, 6]}
        df = pd.DataFrame(data)
        ax = task_func(data)
        result_cumsum = df.cumsum().values.flatten()
        heatmap_data = ax.collections[0].get_array().data.flatten()
        np.testing.assert_array_equal(
            result_cumsum, heatmap_data, "Cumulative sum calculation is incorrect"
        )
    def test_non_numeric_columns_ignored(self):
        data = {"A": [1, 2, 3], "B": ["one", "two", "three"]}
        ax = task_func(data)
        self.assertIsInstance(
            ax, plt.Axes, "The result should be a matplotlib Axes object"
        )
        self.assertEqual(
            len(ax.get_xticklabels()), 1, "Non-numeric columns should be ignored"
        )
    def test_with_positive_numbers(self):
        data = {"A": [1, 2, 3], "B": [4, 5, 6]}
        result = task_func(data)
        self.assertIsInstance(
            result, plt.Axes, "The result should be a matplotlib Axes object"
        )
    def test_with_negative_numbers(self):
        data = {"A": [-1, -2, -3], "B": [-4, -5, -6]}
        result = task_func(data)
        self.assertIsInstance(
            result, plt.Axes, "The result should be a matplotlib Axes object"
        )
    def test_with_mixed_numbers(self):
        data = {"A": [1, -2, 3], "B": [-4, 5, -6]}
        result = task_func(data)
        self.assertIsInstance(
            result, plt.Axes, "The result should be a matplotlib Axes object"
        )
    def test_with_zeroes(self):
        data = {"A": [0, 0, 0], "B": [0, 0, 0]}
        result = task_func(data)
        self.assertIsInstance(
            result, plt.Axes, "The result should be a matplotlib Axes object"
        )
    def test_with_empty_dataframe(self):
        data = {"A": [], "B": []}
        with self.assertRaises(ValueError):
            task_func(data)
    def test_no_numeric_columns(self):
        data = {"A": ["one", "two", "three"], "B": ["four", "five", "six"]}
        with self.assertRaises(ValueError):
            task_func(data)