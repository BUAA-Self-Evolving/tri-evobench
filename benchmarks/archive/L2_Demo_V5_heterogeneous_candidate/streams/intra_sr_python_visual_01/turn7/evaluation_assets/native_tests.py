import unittest
import pandas as pd
class TestCases(unittest.TestCase):
    """Test cases for the task_func function."""
    def test_case_1(self):
        df = pd.DataFrame({"A": [1, 4, 7, 6, 7, 3, 4, 4]})
        df1, ax = task_func(df)
        self.assertIsInstance(ax, plt.Axes)
    def test_case_2(self):
        df = pd.DataFrame({"A": [1, 2, 3, 4, 5], "B": [7, 4, 3, 3, 1]})
        df1, ax = task_func(df)
        self.assertIsInstance(ax, plt.Axes)
        self.assertEqual(len(ax.lines), 2)
    def test_case_3(self):
        df = pd.DataFrame({"A": [5, 6, 2, 9, 7, 3, 2, 2, 8, 1]})
        target_values = [1, 2, 3, 4, 5]
        df1, ax = task_func(df, target_values=target_values)
        mask = df1.isin(target_values) | (df1 == 0)
        self.assertTrue(mask.all().all())
        self.assertIsInstance(ax, plt.Axes)
    def test_case_4(self):
        df = pd.DataFrame({"A": [10, 20, 30, 40, 50], "B": [50, 40, 10, 10, 30]})
        target_values = [10, 20, 30]
        df1, ax = task_func(df, target_values=target_values)
        mask = df1.isin(target_values) | (df1 == 0)
        self.assertTrue(mask.all().all())
        self.assertIsInstance(ax, plt.Axes)
        self.assertEqual(len(ax.lines), 2)
    def test_case_5(self):
        df = pd.DataFrame({"A": [5, 6, 2, 9, 7, 3, 2, 2, 8, 1]})
        df1, ax = task_func(df, target_values=[])
        self.assertTrue(df1.eq(0).all().all())
        self.assertIsInstance(ax, plt.Axes)
    def test_case_7(self):
        df = pd.DataFrame({"A": [5, 6, 2, 9, 7, 3, 2, 2, 8, 1]})
        df1, ax = task_func(df, target_values=[5, 6, 2, 9, 7, 3, 8, 1])
        self.assertTrue(df1.equals(df))
        self.assertIsInstance(ax, plt.Axes)
