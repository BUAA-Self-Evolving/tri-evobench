import unittest
import matplotlib
import matplotlib.pyplot as plt
import pandas as pd
import numpy as np
class TestCases(unittest.TestCase):
    def setUp(self):
        self.data1 = [
            {"A": 10, "B": 15, "C": 12},
            {"A": 12, "B": 20, "C": 14},
            {"A": 15, "B": 18, "C": 15},
            {"A": 11, "B": 17, "C": 13},
        ]
        self.data2 = [
            {"X": 5, "Y": 8},
            {"X": 6, "Y": 7},
            {"X": 7, "Y": 6},
            {"X": 8, "Y": 5},
        ]
        self.data3 = [{"P": 3, "Q": 2, "R": 4, "S": 1}, {"P": 4, "Q": 3, "R": 2, "S": 3}]
        self.data4 = [{"W": 7}, {"W": 8}, {"W": 9}, {"W": 6}]
        self.data5 = [{"M": 1, "N": 3}, {"M": 3, "N": 1}]
    def test_case_1(self):
        # Test for correct Axes instance and labels for a typical data set
        ax = task_func(self.data1)
        self.assertIsInstance(ax, matplotlib.axes.Axes)
        self.assertEqual(ax.get_title(), "Data over Time")
        self.assertEqual(ax.get_xlabel(), "Time")
        self.assertEqual(ax.get_ylabel(), "Data Points")
        self.assertEqual(len(ax.lines), 3)
    def test_case_2(self):
        # Test for different keys across dictionaries in data list
        data = [{"A": 1, "B": 2}, {"B": 3, "C": 4}, {"A": 5, "C": 6}]
        ax = task_func(data)
        self.assertIsInstance(ax, matplotlib.axes.Axes)
        self.assertTrue(len(ax.lines) > 0)
    def test_case_3(self):
        # Test with empty data list
        self.assertIsNone(task_func([]))
    def test_case_4(self):
        # Test with data containing non-numeric values
        data = [{"A": "text", "B": "more text"}, {"A": 1, "B": 2}]
        with self.assertRaises(TypeError):
            task_func(data)
    def test_case_5(self):
        # Test with a single entry in the data list
        data = [{"A": 1, "B": 2}]
        ax = task_func(data)
        self.assertIsInstance(ax, matplotlib.axes.Axes)
        self.assertEqual(len(ax.lines), 2)
    def test_case_6(self):
        # Test focusing on data processing correctness
        data = [
            {"A": 10, "B": 15, "C": 12},
            {"A": 12, "B": 20, "C": 14},
            {"A": 15, "B": 18, "C": 15},
            {"A": 11, "B": 17, "C": 13},
        ]
        ax = task_func(data)
        self.assertIsInstance(ax, matplotlib.axes.Axes)
        # Convert input data to DataFrame for easy comparison
        input_df = pd.DataFrame(data)
        # Iterate through each line in the plot and check against the input data
        for line in ax.lines:
            label = line.get_label()
            _, y_data = line.get_data()
            expected_y_data = input_df[label].values
            # Use numpy to compare the y_data from plot and expected data from input
            np.testing.assert_array_equal(
                y_data, expected_y_data, err_msg=f"Data mismatch for label {label}"
            )
    def tearDown(self):
        plt.close("all")