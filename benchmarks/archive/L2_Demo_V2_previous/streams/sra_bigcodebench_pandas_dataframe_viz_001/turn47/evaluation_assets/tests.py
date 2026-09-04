import unittest
import matplotlib.pyplot as plt
class TestCases(unittest.TestCase):
    
    def test_case_1(self):
        df, ax = task_func()
        self.assertEqual(df.shape, (100, 5))
        self.assertIsInstance(ax, plt.Axes)
        
    def test_case_2(self):
        df, ax = task_func(10, 3)
        self.assertEqual(df.shape, (10, 3))
        self.assertIsInstance(ax, plt.Axes)
    def test_case_3(self):
        df, ax = task_func(50, 2)
        self.assertEqual(df.shape, (50, 2))
        self.assertIsInstance(ax, plt.Axes)
        
    def test_case_4(self):
        df, ax = task_func(150, 6)
        self.assertEqual(df.shape, (150, 6))
        self.assertIsInstance(ax, plt.Axes)
        
    def test_case_5(self):
        df, ax = task_func(5, 10)
        self.assertEqual(df.shape, (5, 10))
        self.assertIsInstance(ax, plt.Axes)