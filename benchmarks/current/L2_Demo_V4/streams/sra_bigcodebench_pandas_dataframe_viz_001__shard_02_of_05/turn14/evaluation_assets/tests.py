import unittest
import numpy as np
import matplotlib
class TestCases(unittest.TestCase):
    def test_case_1(self):
        matrix = np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9]])
        ax = task_func(matrix)
        
        # Asserting the return type
        self.assertIsInstance(ax, matplotlib.axes.Axes)
        
        # Asserting the colormap used
        self.assertEqual(ax.images[0].get_cmap().name, 'hot')
    def test_case_2(self):
        matrix = np.array([[10, 20], [30, 40]])
        ax = task_func(matrix)
        
        self.assertIsInstance(ax, matplotlib.axes.Axes)
        self.assertEqual(ax.images[0].get_cmap().name, 'hot')
    def test_case_3(self):
        matrix = np.array([[1, 1], [1, 1], [1, 1]])
        ax = task_func(matrix)
        
        self.assertIsInstance(ax, matplotlib.axes.Axes)
        self.assertEqual(ax.images[0].get_cmap().name, 'hot')
    def test_case_4(self):
        matrix = np.array([[1]])
        ax = task_func(matrix)
        
        self.assertIsInstance(ax, matplotlib.axes.Axes)
        self.assertEqual(ax.images[0].get_cmap().name, 'hot')
    def test_case_5(self):
        matrix = np.random.rand(5, 5)  # Random 5x5 matrix
        ax = task_func(matrix)
        
        self.assertIsInstance(ax, matplotlib.axes.Axes)
        self.assertEqual(ax.images[0].get_cmap().name, 'hot')