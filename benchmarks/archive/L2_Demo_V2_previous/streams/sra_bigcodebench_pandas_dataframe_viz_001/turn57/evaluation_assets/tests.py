import unittest
import matplotlib
matplotlib.use('Agg')
class TestCases(unittest.TestCase):
    def test_case_1(self):
        fig, ax = task_func()
        self.assertEqual(len(ax.patches), 25)
    def test_case_2(self):
        fig, ax = task_func(3, (0, 50))
        self.assertEqual(len(ax.patches), 9)
    def test_case_3(self):
        fig, ax = task_func(10)
        self.assertEqual(len(ax.patches), 100)
    def test_case_4(self):
        fig, ax = task_func(1, (10, 20))
        self.assertEqual(len(ax.patches), 1)
    def test_case_5(self):
        fig, ax = task_func(2, (5, 15))
        self.assertEqual(len(ax.patches), 4)