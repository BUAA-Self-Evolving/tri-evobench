import unittest
import matplotlib.pyplot as plt
import pandas as pd
from datetime import datetime
import random
class TestCases(unittest.TestCase):
    def setUp(self):
        self.temperatures = pd.DataFrame({
            'temperature': [random.randint(-10, 30) for _ in range(365)],
            'date': pd.date_range(start='01-01-2023', periods=365, tz='America/New_York')
        }).set_index('date')
    def test_basic_functionality(self):
        ax = task_func(self.temperatures)
        self.assertIsInstance(ax, plt.Axes)
    def test_empty_dataframe(self):
        with self.assertRaises(ValueError):
            task_func(pd.DataFrame())
    def test_incorrect_dataframe(self):
        incorrect_df = pd.DataFrame({'temp': [20, 21], 'time': [datetime.now(), datetime.now()]})
        with self.assertRaises(ValueError):
            task_func(incorrect_df)
    def test_data_on_plot(self):
        ax = task_func(self.temperatures)
        self.assertEqual(len(ax.get_lines()[0].get_xdata()), 365)
        self.assertEqual(len(ax.get_lines()[0].get_ydata()), 365)
    def test_plot_labels_and_title(self):
        ax = task_func(self.temperatures)
        self.assertEqual(ax.get_xlabel(), 'Date')
        self.assertEqual(ax.get_ylabel(), 'Temperature (°C)')
        self.assertEqual(ax.get_title(), 'Daily Temperatures in New York')
    
    def test_value_consistency(self):
        ax = task_func(self.temperatures)
        line = ax.get_lines()[0]
        plot_dates = line.get_xdata()
        plot_temperatures = line.get_ydata()
        for date, temperature in zip(plot_dates, plot_temperatures):
            self.assertAlmostEqual(temperature, self.temperatures.at[pd.Timestamp(date), 'temperature'])