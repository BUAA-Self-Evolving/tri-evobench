import unittest
import numpy as np
import matplotlib
matplotlib.use('Agg')
class TestCases(unittest.TestCase):
    def test_case_1(self):
        data = np.array([[1, 2, 3, 4, 4, 3, 7, 1], [6, 2, 3, 4, 3, 4, 4, 1]])
        df, ax = task_func(data)
        self.assertEqual(df.shape, (2, 9))
        self.assertTrue('Average' in df.columns)
        lines = ax.get_lines()
        self.assertEqual(len(lines), 1)
        self.assertListEqual(list(lines[0].get_ydata()), list(df['Average']))
    def test_case_2(self):
        data = np.array([[5, 5, 5, 5, 5, 5, 5, 5]])
        df, ax = task_func(data)
        self.assertEqual(df.shape, (1, 9))
        self.assertTrue('Average' in df.columns)
        lines = ax.get_lines()
        self.assertEqual(len(lines), 1)
        self.assertListEqual(list(lines[0].get_ydata()), list(df['Average']))
    def test_case_3(self):
        data = np.array([[0, 0, 0, 0, 0, 0, 0, 0], [10, 10, 10, 10, 10, 10, 10, 10]])
        df, ax = task_func(data)
        self.assertEqual(df.shape, (2, 9))
        self.assertTrue('Average' in df.columns)
        lines = ax.get_lines()
        self.assertEqual(len(lines), 1)
        self.assertListEqual(list(lines[0].get_ydata()), list(df['Average']))
    def test_case_4(self):
        data = np.array([[1, 2, 3, 4, 5, 6, 7, 8]])
        df, ax = task_func(data)
        self.assertEqual(df.shape, (1, 9))
        self.assertTrue('Average' in df.columns)
        lines = ax.get_lines()
        self.assertEqual(len(lines), 1)
        self.assertListEqual(list(lines[0].get_ydata()), list(df['Average']))
    def test_case_5(self):
        data = np.array([[8, 7, 6, 5, 4, 3, 2, 1]])
        df, ax = task_func(data)
        self.assertEqual(df.shape, (1, 9))
        self.assertTrue('Average' in df.columns)
        lines = ax.get_lines()
        self.assertEqual(len(lines), 1)
        self.assertListEqual(list(lines[0].get_ydata()), list(df['Average']))