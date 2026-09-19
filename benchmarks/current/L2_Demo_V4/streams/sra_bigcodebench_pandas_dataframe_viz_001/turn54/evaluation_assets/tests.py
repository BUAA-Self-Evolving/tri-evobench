import unittest
import numpy as np
import matplotlib
matplotlib.use('Agg')
class TestCases(unittest.TestCase):
    def test_case_1(self):
        data = np.array([[1, 2, 3, 4, 4, 3, 7, 1], [6, 2, 3, 4, 3, 4, 4, 1]])
        df, ax = task_func(data)
        # Testing the DataFrame
        self.assertEqual(df.shape, (2, 9))
        self.assertIn('Average', df.columns)
        self.assertAlmostEqual(df['Average'][0], 3.125, places=3)
        self.assertAlmostEqual(df['Average'][1], 3.375, places=3)
        # Testing the plot
        self.assertEqual(ax.get_title(), '')
        self.assertEqual(ax.get_xlabel(), '')
        self.assertEqual(ax.get_ylabel(), 'Average')
        self.assertEqual(len(ax.lines), 1)
    def test_case_2(self):
        data = np.array([[1, 1, 1, 1, 1, 1, 1, 1]])
        df, ax = task_func(data)
        # Testing the DataFrame
        self.assertEqual(df.shape, (1, 9))
        self.assertIn('Average', df.columns)
        self.assertEqual(df['Average'][0], 1.0)
        # Testing the plot
        self.assertEqual(len(ax.lines), 1)
    def test_case_3(self):
        data = np.array([[1, 2, 3, 4, 5, 6, 7, 8], [8, 7, 6, 5, 4, 3, 2, 1]])
        df, ax = task_func(data)
        # Testing the DataFrame
        self.assertEqual(df.shape, (2, 9))
        self.assertIn('Average', df.columns)
        self.assertEqual(df['Average'][0], 4.5)
        self.assertEqual(df['Average'][1], 4.5)
        # Testing the plot
        self.assertEqual(len(ax.lines), 1)
    def test_case_4(self):
        data = np.array([[0, 0, 0, 0, 0, 0, 0, 0], [10, 10, 10, 10, 10, 10, 10, 10]])
        df, ax = task_func(data)
        # Testing the DataFrame
        self.assertEqual(df.shape, (2, 9))
        self.assertIn('Average', df.columns)
        self.assertEqual(df['Average'][0], 0.0)
        self.assertEqual(df['Average'][1], 10.0)
        # Testing the plot
        self.assertEqual(len(ax.lines), 1)
    def test_case_5(self):
        data = np.array([[5, 5, 5, 5, 5, 5, 5, 5]])
        df, ax = task_func(data)
        # Testing the DataFrame
        self.assertEqual(df.shape, (1, 9))
        self.assertIn('Average', df.columns)
        self.assertEqual(df['Average'][0], 5.0)
        # Testing the plot
        self.assertEqual(len(ax.lines), 1)