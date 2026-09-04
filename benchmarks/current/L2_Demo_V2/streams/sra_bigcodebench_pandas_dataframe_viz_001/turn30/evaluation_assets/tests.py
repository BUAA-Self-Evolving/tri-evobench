import numpy as np
import pandas as pd
import unittest
import matplotlib.pyplot as plt
class TestCases(unittest.TestCase):
    def setUp(self):
        # Setup common for all tests
        self.input_df = pd.DataFrame({"A": [1, 2, 3], "B": [4, 5, 6]})
        self.expected_df = pd.DataFrame({"A": [1, 3, 6], "B": [4, 9, 15]})
    def test_case_1(self):
        # Test basic case
        output_df, _ = task_func(self.input_df)
        pd.testing.assert_frame_equal(output_df, self.expected_df)
    def test_case_2(self):
        # Test cumulative sum correctness for a case with negative values
        input_df_neg = pd.DataFrame({"A": [1, -2, 3], "B": [-4, 5, -6]})
        expected_df_neg = pd.DataFrame({"A": [1, -1, 2], "B": [-4, 1, -5]})
        output_df_neg, _ = task_func(input_df_neg)
        pd.testing.assert_frame_equal(output_df_neg, expected_df_neg)
    def test_case_3(self):
        # Test bar chart properties
        _, fig = task_func(self.input_df)
        self.assertIsInstance(fig, plt.Figure)
        ax = fig.axes[0]  # Get the Axes object from the figure
        # Verify the title, x-label, and y-label
        self.assertEqual(ax.get_title(), "Cumulative Sum per Column")
        self.assertEqual(ax.get_xlabel(), "Index")
        self.assertEqual(ax.get_ylabel(), "Cumulative Sum")
        # Ensure that a legend is present and contains the correct labels
        legend_labels = [text.get_text() for text in ax.get_legend().get_texts()]
        expected_labels = self.input_df.columns.tolist()
        self.assertEqual(legend_labels, expected_labels)
    def test_case_4(self):
        # Test with an empty DataFrame
        empty_df = pd.DataFrame()
        with self.assertRaises(Exception):
            task_func(empty_df)
    def test_case_5(self):
        # Test with DataFrame containing NaN values
        nan_df = pd.DataFrame({"A": [1, np.nan, 3], "B": [4, 5, np.nan]})
        nan_df_cumsum = nan_df.cumsum()
        output_nan_df, _ = task_func(nan_df)
        pd.testing.assert_frame_equal(output_nan_df, nan_df_cumsum)
    def test_case_6(self):
        # Test with DataFrame containing all zeros
        zeros_df = pd.DataFrame({"A": [0, 0, 0], "B": [0, 0, 0]})
        expected_zeros_df = pd.DataFrame({"A": [0, 0, 0], "B": [0, 0, 0]})
        output_zeros_df, _ = task_func(zeros_df)
        pd.testing.assert_frame_equal(output_zeros_df, expected_zeros_df)
    def test_case_7(self):
        # Test with a DataFrame containing only one row
        one_row_df = pd.DataFrame({"A": [1], "B": [2]})
        expected_one_row_df = pd.DataFrame({"A": [1], "B": [2]})
        output_one_row_df, _ = task_func(one_row_df)
        pd.testing.assert_frame_equal(output_one_row_df, expected_one_row_df)