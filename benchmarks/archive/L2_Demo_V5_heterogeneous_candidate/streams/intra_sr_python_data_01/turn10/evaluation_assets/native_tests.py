import unittest
import numpy as np
import matplotlib
class TestCases(unittest.TestCase):
    """Test cases for the task_func function."""
    def test_case_1(self):
        data = np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9]])
        df, ax = task_func(data)
        self.assertTrue(isinstance(df, pd.DataFrame))
        self.assertTrue(isinstance(ax, matplotlib.axes.Axes))
        np.testing.assert_array_equal(
            df.loc[:, [col for col in df.columns if col.startswith("Feature")]].values,
            zscore(data, axis=1),
        )
        self.assertTrue("Mean" in df.columns)
    def test_case_2(self):
        data = np.array([[0, 0, 1], [0, 1, 0], [1, 0, 0]])
        df, ax = task_func(data)
        self.assertTrue(isinstance(df, pd.DataFrame))
        self.assertTrue(isinstance(ax, matplotlib.axes.Axes))
        np.testing.assert_array_equal(
            df.loc[:, [col for col in df.columns if col.startswith("Feature")]].values,
            zscore(data, axis=1),
        )
        self.assertTrue("Mean" in df.columns)
    def test_case_3(self):
        data = np.array([[3, 5, 7, 1000], [200, 5, 7, 1], [1, -9, 14, 700]])
        df, ax = task_func(data)
        self.assertTrue(isinstance(df, pd.DataFrame))
        self.assertTrue(isinstance(ax, matplotlib.axes.Axes))
        np.testing.assert_array_equal(
            df.loc[:, [col for col in df.columns if col.startswith("Feature")]].values,
            zscore(data, axis=1),
        )
        self.assertTrue("Mean" in df.columns)
    def test_case_4(self):
        data = np.array(
            [
                [1, 2, 3, 4, 5, 4, 3, 2, 1],
            ]
        )
        df, ax = task_func(data)
        self.assertTrue(isinstance(df, pd.DataFrame))
        self.assertTrue(isinstance(ax, matplotlib.axes.Axes))
        np.testing.assert_array_equal(
            df.loc[:, [col for col in df.columns if col.startswith("Feature")]].values,
            zscore(data, axis=1),
        )
        self.assertTrue("Mean" in df.columns)
    def test_case_5(self):
        data = np.array([[1], [1], [1]])
        df, ax = task_func(data)
        self.assertTrue(isinstance(df, pd.DataFrame))
        self.assertTrue(isinstance(ax, matplotlib.axes.Axes))
        np.testing.assert_array_equal(
            df.loc[:, [col for col in df.columns if col.startswith("Feature")]].values,
            zscore(data, axis=1),
        )
        self.assertTrue("Mean" in df.columns)
