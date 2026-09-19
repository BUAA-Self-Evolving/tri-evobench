import unittest
import pandas as pd
from matplotlib.figure import Figure
LABELS = ['A', 'B', 'C', 'D', 'E']
class TestCases(unittest.TestCase):
    def test_case_1(self):
        fig = task_func()
        self.assertIsInstance(fig, Figure)
        ax = fig.axes[0]
        self.assertEqual(len(ax.patches), 5 * len(LABELS))  # 5 bars for each category
    def test_case_2(self):
        fig = task_func(num_rows=10)
        self.assertIsInstance(fig, Figure)
        ax = fig.axes[0]
        self.assertEqual(len(ax.patches), 10 * len(LABELS))  # 10 bars for each category
    def test_case_3(self):
        fig = task_func(rand_range=(10, 50))
        self.assertIsInstance(fig, Figure)
        ax = fig.axes[0]
        for bar in ax.patches:
            self.assertTrue(10 <= bar.get_height() <= 50)
    def test_case_4(self):
        fig = task_func(num_rows=3, rand_range=(20, 30))
        self.assertIsInstance(fig, Figure)
        ax = fig.axes[0]
        self.assertEqual(len(ax.patches), 3 * len(LABELS))  # 3 bars for each category
        for bar in ax.patches:
            self.assertTrue(20 <= bar.get_height() <= 30)
    def test_case_5(self):
        fig = task_func(num_rows=7, rand_range=(5, 15))
        self.assertIsInstance(fig, Figure)
        ax = fig.axes[0]
        self.assertEqual(len(ax.patches), 7 * len(LABELS))  # 7 bars for each category
        for bar in ax.patches:
            self.assertTrue(5 <= bar.get_height() <= 15)