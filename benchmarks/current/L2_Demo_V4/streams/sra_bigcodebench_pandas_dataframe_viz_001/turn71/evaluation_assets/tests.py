import unittest
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
class TestCases(unittest.TestCase):
    def setUp(self):
        np.random.seed(42)
        self.df = pd.DataFrame({
            'Date': pd.date_range(start='1/1/2021', periods=10),
            'Sales': np.random.randint(100, 2000, size=10)
        })
    def test_return_type(self):
    # Adjusted to include more data points
        np.random.seed(42)
        large_df = pd.DataFrame({
            'Date': pd.date_range(start='1/1/2021', periods=30),
            'Sales': np.random.randint(100, 2000, size=30)
        })
        ax = task_func(large_df)
        self.assertIsInstance(ax, plt.Axes)
        
    def test_value(self):
        # Adjusted to include more data points
        ax = task_func(self.df)
        # Retrieve the line plot data
        # Assuming 'ax' is the Axes object returned by your function 'task_func'
        # Retrieve the line plot data
        line = ax.get_lines()[0]  # Get the first (and likely only) line plot
        sales = line.get_ydata()
        actual_sales = [str(int(sale)) for sale in sales]
        expect = ['1226', '1559', '960', '1394', '1230', '1195', '1824', '1144', '1738', '221']
        self.assertEqual(actual_sales, expect, "DataFrame contents should match the expected output")
        
    def test_plot_title_and_labels(self):
        # Adjusted to include more data points
        np.random.seed(42)
        large_df = pd.DataFrame({
            'Date': pd.date_range(start='1/1/2021', periods=30),
            'Sales': np.random.randint(100, 2000, size=30)
        })
        ax = task_func(large_df)
        self.assertEqual(ax.get_title(), 'Daily Turnover')
        self.assertEqual(ax.get_ylabel(), 'Sales')
    def test_invalid_input(self):
        with self.assertRaises(ValueError):
            task_func(pd.DataFrame({'a': [1, 2], 'b': [3, 4]}))
    def test_empty_dataframe(self):
        with self.assertRaises(ValueError):
            task_func(pd.DataFrame({'Date': [], 'Sales': []}))
    def test_date_conversion(self):
        df_with_string_dates = self.df.copy()
        df_with_string_dates['Date'] = df_with_string_dates['Date'].dt.strftime('%Y-%m-%d')
        ax = task_func(df_with_string_dates)
        self.assertIsInstance(ax, plt.Axes)