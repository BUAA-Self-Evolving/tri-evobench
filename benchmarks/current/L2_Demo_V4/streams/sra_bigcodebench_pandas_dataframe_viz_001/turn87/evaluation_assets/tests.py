import unittest
import matplotlib
import matplotlib.pyplot as plt
from datetime import datetime
import numpy as np
class TestCases(unittest.TestCase):
    def setUp(self):
        self.data = [
            [datetime(2022, 1, 1), -5, 80, 10, 0],
            [datetime(2022, 1, 2), -3, 85, 12, 0.5],
            [datetime(2022, 1, 3), -2, 83, 15, 0],
            [datetime(2022, 1, 4), -1, 82, 13, 0.2],
            [datetime(2022, 1, 5), 0, 80, 11, 0.1],
        ]
    def test_case_1(self):
        # Testing the 'Temperature' column
        result = task_func("Temperature", self.data)
        self.assertEqual(result["sum"], -11)
        self.assertEqual(result["mean"], -2.2)
        self.assertEqual(result["min"], -5)
        self.assertEqual(result["max"], 0)
        self.assertIsInstance(result["plot"], matplotlib.container.BarContainer)
    def test_case_2(self):
        # Testing the 'Humidity' column
        result = task_func("Humidity", self.data)
        self.assertEqual(result["sum"], 410)
        self.assertEqual(result["mean"], 82)
        self.assertEqual(result["min"], 80)
        self.assertEqual(result["max"], 85)
        self.assertIsInstance(result["plot"], matplotlib.container.BarContainer)
    def test_case_3(self):
        # Testing the 'Wind Speed' column
        result = task_func("Wind Speed", self.data)
        self.assertEqual(result["sum"], 61)
        self.assertEqual(result["mean"], 12.2)
        self.assertEqual(result["min"], 10)
        self.assertEqual(result["max"], 15)
        self.assertIsInstance(result["plot"], matplotlib.container.BarContainer)
    def test_case_4(self):
        # Testing the 'Precipitation' column
        result = task_func("Precipitation", self.data)
        self.assertAlmostEqual(result["sum"], 0.8, places=6)
        self.assertAlmostEqual(result["mean"], 0.16, places=6)
        self.assertAlmostEqual(result["min"], 0, places=6)
        self.assertAlmostEqual(result["max"], 0.5, places=6)
        self.assertIsInstance(result["plot"], matplotlib.container.BarContainer)
    def test_case_5(self):
        # Testing with empty data
        result = task_func("Temperature", [])
        self.assertTrue(np.isnan(result["mean"]))
        self.assertEqual(result["sum"], 0)
        self.assertTrue(
            np.isinf(result["min"]) and result["min"] > 0
        )  # Checking for positive infinity for min
        self.assertTrue(
            np.isinf(result["max"]) and result["max"] < 0
        )  # Checking for negative infinity for max
        self.assertIsInstance(result["plot"], matplotlib.container.BarContainer)
    def tearDown(self):
        plt.close("all")