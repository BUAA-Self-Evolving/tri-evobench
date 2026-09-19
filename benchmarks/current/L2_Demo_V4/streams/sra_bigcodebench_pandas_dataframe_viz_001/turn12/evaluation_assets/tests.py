import unittest
import pandas as pd
import matplotlib.pyplot as plt
class TestCases(unittest.TestCase):
    
    def test_case_1(self):
        data = {'a': [1, 2, 3], 'b': [4, 5, 6], 'c': [7, 8, 9]}
        ax = task_func(data)
        self.assertIsInstance(ax, plt.Axes)
        self.assertEqual(ax.title.get_text(), 'Correlation Matrix')
        
    def test_case_2(self):
        data = {'a': [1, 2, 3], 'b': [-4, -5, -6], 'c': [-7, -8, -9]}
        ax = task_func(data)
        self.assertIsInstance(ax, plt.Axes)
        self.assertEqual(ax.title.get_text(), 'Correlation Matrix')
        
    def test_case_3(self):
        data = {'a': [1, 2, 3], 'b': [4, 5, 6], 'c': [-7, -8, -9]}
        ax = task_func(data)
        self.assertIsInstance(ax, plt.Axes)
        self.assertEqual(ax.title.get_text(), 'Correlation Matrix')
        
    def test_case_4(self):
        data = {'a': [1, 1, 1], 'b': [2, 2, 2], 'c': [3, 3, 3]}
        ax = task_func(data)
        self.assertIsInstance(ax, plt.Axes)
        self.assertEqual(ax.title.get_text(), 'Correlation Matrix')
        
    def test_case_5(self):
        data = {'a': [1, 2, None], 'b': [4, None, 6], 'c': [None, 8, 9]}
        ax = task_func(data)
        self.assertIsInstance(ax, plt.Axes)
        self.assertEqual(ax.title.get_text(), 'Correlation Matrix')