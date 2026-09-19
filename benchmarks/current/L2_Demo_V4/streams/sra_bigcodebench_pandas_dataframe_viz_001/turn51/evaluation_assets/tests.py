import unittest
import numpy as np
import matplotlib.pyplot as plt
class TestCases(unittest.TestCase):
    """Test cases for the function task_func."""
    def test_basic_functionality(self):
        """Test the basic functionality of the function."""
        arr = np.array([[i + j for i in range(3)] for j in range(5)])
        ax = task_func(arr)
        # Check if the function returns Axes object
        self.assertIsInstance(ax, plt.Axes)
        # Check the title of the plot
        self.assertEqual(ax.get_title(), "Time Series of Row Sums")
        # Check if the data plotted matches the expected sum of rows
        y_data = [line.get_ydata() for line in ax.get_lines()][0]
        expected_sums = arr.sum(axis=1)
        np.testing.assert_array_equal(y_data, expected_sums)
    def test_empty_array(self):
        """Test the function with an empty array."""
        arr = np.array([])
        ax = task_func(arr)
        # Check if the function returns Axes object
        self.assertIsInstance(ax, plt.Axes)
        # Check the title of the plot
        self.assertEqual(ax.get_title(), "Time Series of Row Sums")
        # Check if the data plotted is empty
        lines = ax.get_lines()
        self.assertEqual(len(lines), 0)
    def test_single_row_array(self):
        """Test the function with a single row array."""
        arr = np.array([[1, 2, 3]])
        ax = task_func(arr)
        # Check if the function returns Axes object
        self.assertIsInstance(ax, plt.Axes)
        # Check the title of the plot
        self.assertEqual(ax.get_title(), "Time Series of Row Sums")
        # Check if the data plotted matches the expected sum of the single row
        y_data = [line.get_ydata() for line in ax.get_lines()][0]
        expected_sum = arr.sum(axis=1)
        np.testing.assert_array_equal(y_data, expected_sum)
    def test_negative_values(self):
        """Test the function with negative values."""
        arr = np.array([[-1, -2, -3], [-4, -5, -6]])
        ax = task_func(arr)
        # Check if the function returns Axes object
        self.assertIsInstance(ax, plt.Axes)
        # Check the title of the plot
        self.assertEqual(ax.get_title(), "Time Series of Row Sums")
        # Check if the data plotted matches the expected sum of rows
        y_data = [line.get_ydata() for line in ax.get_lines()][0]
        expected_sums = arr.sum(axis=1)
        np.testing.assert_array_equal(y_data, expected_sums)
    def test_zero_values(self):
        """Test the function with zero values."""
        arr = np.array([[0, 0, 0], [0, 0, 0], [0, 0, 0]])
        ax = task_func(arr)
        # Check if the function returns Axes object
        self.assertIsInstance(ax, plt.Axes)
        # Check the title of the plot
        self.assertEqual(ax.get_title(), "Time Series of Row Sums")
        # Check if the data plotted matches the expected sum of rows
        y_data = [line.get_ydata() for line in ax.get_lines()][0]
        expected_sums = arr.sum(axis=1)
        np.testing.assert_array_equal(y_data, expected_sums)
    def tearDown(self):
        plt.close()