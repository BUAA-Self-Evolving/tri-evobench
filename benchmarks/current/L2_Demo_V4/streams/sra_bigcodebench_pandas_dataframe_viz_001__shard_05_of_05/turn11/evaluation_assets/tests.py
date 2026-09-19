import unittest
import numpy as np
class TestCases(unittest.TestCase):
    """Test cases for the task_func function."""
    def test_case_1(self):
        data = np.array([[6, 8, 1, 3, 4], [-1, 0, 3, 5, 1]])
        df, ax = task_func(data)
        self.assertEqual(df.shape, (2, 3))
        self.assertTrue("Mean" in df.columns)
        self.assertEqual(ax.get_xlabel(), "Number of Components")
        self.assertEqual(ax.get_ylabel(), "Cumulative Explained Variance")
    def test_case_2(self):
        data = np.array([[1, 2], [3, 4], [5, 6]])
        df, ax = task_func(data)
        self.assertEqual(df.shape, (3, 3))
        self.assertTrue("Mean" in df.columns)
        self.assertEqual(ax.get_xlabel(), "Number of Components")
        self.assertEqual(ax.get_ylabel(), "Cumulative Explained Variance")
    # Additional test cases
    def test_case_3(self):
        data = np.array([[1, 2], [3, 4], [5, 6]])
        df, ax = task_func(data)
        expected_columns = min(data.shape) + 1
        self.assertEqual(df.shape[1], expected_columns)
        self.assertTrue("Mean" in df.columns)
        self.assertEqual(ax.get_xlabel(), "Number of Components")
        self.assertEqual(ax.get_ylabel(), "Cumulative Explained Variance")
    def test_case_4(self):
        data = np.array([[1, 2], [3, 4], [5, 6]])
        df, ax = task_func(data)
        expected_columns = min(data.shape) + 1
        self.assertEqual(df.shape[1], expected_columns)
        self.assertTrue("Mean" in df.columns)
        self.assertEqual(ax.get_xlabel(), "Number of Components")
        self.assertEqual(ax.get_ylabel(), "Cumulative Explained Variance")
    def test_case_5(self):
        data = np.array([[1, 2], [3, 4], [5, 6]])
        df, ax = task_func(data)
        expected_columns = min(data.shape) + 1
        self.assertEqual(df.shape[1], expected_columns)
        self.assertTrue("Mean" in df.columns)
        self.assertTrue("Component 1" in df.columns)
        self.assertTrue("Component 2" in df.columns)
        self.assertEqual(ax.get_xlabel(), "Number of Components")
        self.assertEqual(ax.get_ylabel(), "Cumulative Explained Variance")