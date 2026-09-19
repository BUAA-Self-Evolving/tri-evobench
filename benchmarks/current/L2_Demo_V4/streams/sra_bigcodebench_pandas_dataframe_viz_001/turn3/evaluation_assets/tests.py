import unittest
import pandas as pd
from matplotlib import pyplot as plt
class TestCases(unittest.TestCase):
    
    def test_with_all_categories(self):
        """Test with all categories present."""
        data = {'Category': ['A', 'B', 'B', 'C', 'A', 'D', 'E', 'E', 'D']}
        ax = task_func(data, 'Category')
        self.assertIsInstance(ax, plt.Axes)
        self.assertEqual(ax.get_xlabel(), 'Category')
        self.assertEqual(ax.get_ylabel(), 'Count')
        self.assertEqual(ax.get_title(), 'Distribution of Category')
        self.assertEqual(len(ax.get_xticks()), 5)  # Check the number of x-axis ticks instead
    def test_with_missing_categories(self):
        """Test with some categories missing."""
        data = {'Category': ['A', 'A', 'B', 'C']}
        ax = task_func(data, 'Category')
        self.assertIsInstance(ax, plt.Axes)
        self.assertEqual(len(ax.get_xticks()), 5)  # Ensure all categories are accounted for, including missing ones
    def test_with_unexpected_category(self):
        """Test with a category not in predefined list."""
        data = {'Category': ['F', 'A', 'B']}  # 'F' is not a predefined category
        ax = task_func(data, 'Category')
        self.assertIsInstance(ax, plt.Axes)
        self.assertEqual(len(ax.get_xticks()), 5)  # 'F' is ignored, only predefined categories are considered