import unittest
import pandas as pd
from matplotlib.axes import Axes
import matplotlib.pyplot as plt
class TestCases(unittest.TestCase):
    """Test cases for task_func."""
    def test_valid_data(self):
        """Test with valid data."""
        data = {"Salary_String": ["1,000", "2,000", "3,000"], "Experience": [1, 2, 3]}
        result = task_func(data)
        self.assertIsInstance(result, Axes)
    def test_missing_key(self):
        """Test with missing key in input dictionary."""
        data = {"Salary_String": ["1,000", "2,000", "3,000"]}
        with self.assertRaises(ValueError):
            task_func(data)
    def test_empty_data(self):
        """Test with empty data."""
        data = {"Salary_String": [], "Experience": []}
        result = task_func(data)
        self.assertIsInstance(result, Axes)
    def test_invalid_salary_format(self):
        """Test with invalid salary format."""
        data = {
            "Salary_String": ["1.000", "2,000", "Three Thousand"],
            "Experience": [1, 2, 3],
        }
        with self.assertRaises(ValueError):
            task_func(data)
    def test_mismatched_lengths(self):
        """Test with mismatched lengths of salary and experience arrays."""
        data = {"Salary_String": ["1,000", "2,000"], "Experience": [1, 2, 3]}
        with self.assertRaises(ValueError):
            task_func(data)
    def tearDown(self):
        plt.close("all")