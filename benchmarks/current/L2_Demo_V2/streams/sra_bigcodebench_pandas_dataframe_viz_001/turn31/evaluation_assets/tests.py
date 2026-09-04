import unittest
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
class TestCases(unittest.TestCase):
    """Test cases for the function task_func."""
    def test_high_correlation(self):
        """
        Test if the function correctly identifies and plots the pair of columns with the highest positive correlation.
        """
        np.random.seed(0)  # Set a fixed seed for reproducibility
        df = pd.DataFrame(
            {"A": np.arange(100), "B": np.arange(100) * 2, "C": np.random.rand(100)}
        )
        ax = task_func(df)
        corr = df.corr()
        abs_corr = corr.abs()
        max_corr = abs_corr.unstack().dropna().nlargest(3).iloc[-1]
        expected_pair = np.where(abs_corr == max_corr)
        expected_labels = (
            df.columns[expected_pair[0][0]],
            df.columns[expected_pair[1][0]],
        )
        self.assertEqual((ax.get_xlabel(), ax.get_ylabel()), expected_labels)
    def test_no_correlation(self):
        """
        Test if the function handles a case where there is no significant correlation between columns.
        """
        np.random.seed(1)
        df = pd.DataFrame(
            {
                "A": np.random.rand(100),
                "B": np.random.rand(100),
                "C": np.random.rand(100),
            }
        )
        ax = task_func(df)
        self.assertIsInstance(ax, plt.Axes)
    def test_negative_correlation(self):
        """
        Test if the function correctly identifies and plots the pair of columns with the highest absolute correlation,
        including negative correlations.
        """
        np.random.seed(2)
        df = pd.DataFrame(
            {"A": np.arange(100), "B": np.random.rand(100), "C": -np.arange(100) + 50}
        )
        ax = task_func(df)
        corr = df.corr()
        # Get the pair with the highest absolute correlation excluding self-correlations
        abs_corr = corr.abs()
        max_corr = abs_corr.unstack().dropna().nlargest(3).iloc[-1]
        expected_pair = np.where(abs_corr == max_corr)
        expected_labels = (
            df.columns[expected_pair[0][0]],
            df.columns[expected_pair[1][0]],
        )
        self.assertEqual((ax.get_xlabel(), ax.get_ylabel()), expected_labels)
    def test_single_column(self):
        """
        Test if the function raises a ValueError when provided with a DataFrame containing only one column.
        """
        np.random.seed(3)
        df = pd.DataFrame({"A": np.random.rand(100)})
        with self.assertRaises(ValueError):
            task_func(df)
    def test_non_numeric_columns(self):
        """
        Test if the function raises a TypeError when provided with a DataFrame containing non-numeric columns.
        """
        np.random.seed(4)
        df = pd.DataFrame(
            {"A": np.random.rand(100), "B": ["text"] * 100, "C": np.random.rand(100)}
        )
        with self.assertRaises(TypeError):
            task_func(df)
    def test_empty_dataframe(self):
        """
        Test if the function raises a ValueError when provided with an empty DataFrame.
        """
        df = pd.DataFrame()  # Create an empty DataFrame
        with self.assertRaises(ValueError):
            task_func(df)