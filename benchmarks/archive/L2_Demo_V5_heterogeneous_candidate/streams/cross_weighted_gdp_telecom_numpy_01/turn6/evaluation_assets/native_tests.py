import unittest
import pandas as pd
class TestCases(unittest.TestCase):
    """Test cases for the task_func function."""
    def test_case_1(self):
        df = pd.DataFrame(
            {
                "A": [1, 2, 3, 4, 3, 2, 2, 1],
                "B": [7, 8, 9, 1, 2, 3, 5, 6],
                "C": [9, 7, 3, 1, 8, 6, 2, 1],
            }
        )
        transformed_df, fig = task_func(df)
        self.assertEqual(transformed_df.shape, df.shape)
    def test_case_2(self):
        df = pd.DataFrame({"A": [1, 1, 1], "B": [3, 3, 3], "C": [4, 4, 4]})
        transformed_df, fig = task_func(df)
        self.assertEqual(transformed_df.shape, df.shape)
        self.assertEqual(len(fig.axes[0].lines), 0)
        pd.testing.assert_frame_equal(transformed_df, df)
    def test_case_3(self):
        df = pd.DataFrame(
            {
                "A": [1, 7, 5, 4],
                "B": [3, 11, 1, 29],
                "C": [4, 9, 8, 4],
                "D": [16, 12, 20, 8],
            }
        )
        transformed_df, fig = task_func(df)
        self.assertEqual(transformed_df.shape, df.shape)
        self.assertEqual(len(fig.axes[0].lines), 3)
    def test_case_4(self):
        df = pd.DataFrame(
            {
                "E": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                "F": [11, 12, 13, 14, 15, 16, 17, 18, 19, 20],
            }
        )
        transformed_df, fig = task_func(df)
        self.assertEqual(transformed_df.shape, df.shape)
        self.assertEqual(len(fig.axes[0].lines), 1)
    def test_case_5(self):
        df = pd.DataFrame(
            {
                "A": [0, 0, 0, 0],
            }
        )
        with self.assertRaises(ValueError):
            transformed_df, _ = task_func(df)
    def test_case_6(self):
        df = pd.DataFrame(
            {
                "A": [1, 2, 3, -4],
            }
        )
        with self.assertRaises(ValueError):
            transformed_df, _ = task_func(df)
