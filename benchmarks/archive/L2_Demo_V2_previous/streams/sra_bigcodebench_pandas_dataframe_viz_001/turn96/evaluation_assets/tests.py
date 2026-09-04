import unittest
import pandas as pd
import inspect
from unittest.mock import patch
class TestCases(unittest.TestCase):
    def test_single_function(self):
        def sample_function(x): pass
        df = task_func([sample_function])
        self.assertEqual(df.loc['sample_function', 'Number of Arguments'], 1)
    def test_multiple_functions(self):
        def f(x): pass
        def g(x, y): pass
        df = task_func([f, g])
        self.assertEqual(df.loc['f', 'Number of Arguments'], 1)
        self.assertEqual(df.loc['g', 'Number of Arguments'], 2)
    def test_no_arguments_function(self):
        def no_arg_func(): pass
        df = task_func([no_arg_func])
        self.assertEqual(df.loc['no_arg_func', 'Number of Arguments'], 0)
    def test_lambda_functions(self):
        lambda_func = lambda x, y: x + y
        with self.assertRaises(ValueError):
            df = task_func([lambda_func])
    
    def test_function_with_defaults(self):
        def func_with_defaults(x, y=2): pass
        df = task_func([func_with_defaults])
        self.assertEqual(df.loc['func_with_defaults', 'Number of Arguments'], 2)
    @patch('matplotlib.pyplot.show')
    def test_plot_called(self, mock_show):
        def sample_function(x): pass
        task_func([sample_function])
        mock_show.assert_called_once()