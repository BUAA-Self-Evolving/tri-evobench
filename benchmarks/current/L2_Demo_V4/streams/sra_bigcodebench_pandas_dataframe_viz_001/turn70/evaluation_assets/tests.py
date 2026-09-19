import unittest
import pandas as pd
from matplotlib.axes import Axes
class TestCases(unittest.TestCase):
    """Test cases for task_func"""
    def test_default_data(self):
        """Test task_func with its default data."""
        result = task_func()
        self.assertIsInstance(result, Axes)
    def test_custom_data(self):
        """Test task_func with custom data."""
        custom_data = {
            "Weight_String": ["50.5", "55.7", "60.2"],
            "Height": [150, 155, 160],
        }
        result = task_func(custom_data)
        self.assertIsInstance(result, Axes)
    def test_incorrect_data_type(self):
        """Test task_func with incorrect data types in Weight_String."""
        incorrect_data = {
            "Weight_String": [
                60.5,
                65.7,
                70.2,
            ],  # Intentionally using floats instead of strings
            "Height": [160, 165, 170],
        }
        with self.assertRaises(ValueError):
            task_func(incorrect_data)
    def test_empty_data(self):
        """Test task_func with empty data."""
        empty_data = {"Weight_String": [], "Height": []}
        result = task_func(empty_data)
        self.assertIsInstance(result, Axes)
    def test_mismatched_data_length(self):
        """Test task_func with mismatched lengths of Weight_String and Height."""
        mismatched_data = {
            "Weight_String": ["60.5", "65.7"],  # Less weights than heights
            "Height": [160, 165, 170],
        }
        with self.assertRaises(ValueError):
            task_func(mismatched_data)