import unittest
import numpy as np
class TestCases(unittest.TestCase):
    """Test cases for the task_func function."""
    def test_case_1(self):
        data = np.array([[6, 8, 1, 3, 4], [-1, 0, 3, 5, 1]])
        df, ax = task_func(data)
        # Check the dataframe structure and values
        self.assertTrue(isinstance(df, pd.DataFrame))
        self.assertListEqual(
            list(df.columns),
            ["Feature 1", "Feature 2", "Feature 3", "Feature 4", "Feature 5", "Mean"],
        )
        self.assertAlmostEqual(df["Mean"].iloc[0], 0.2)
        self.assertAlmostEqual(df["Mean"].iloc[1], -0.2)
        # Check the histogram plot
        self.assertEqual(ax.get_title(), "Distribution of Means")
        self.assertIsNotNone(ax.patches)  # Check if histogram bars exist
    def test_case_2(self):
        data = np.array([[1, 2, 3, 4, 5], [5, 4, 3, 2, 1]])
        df, ax = task_func(data)
        # Check the dataframe structure and values
        self.assertTrue(isinstance(df, pd.DataFrame))
        self.assertListEqual(
            list(df.columns),
            ["Feature 1", "Feature 2", "Feature 3", "Feature 4", "Feature 5", "Mean"],
        )
        self.assertAlmostEqual(df["Mean"].iloc[0], 0.0)
        self.assertAlmostEqual(df["Mean"].iloc[1], 0.0)
        # Check the histogram plot
        self.assertEqual(ax.get_title(), "Distribution of Means")
        self.assertIsNotNone(ax.patches)  # Check if histogram bars exist
    def test_case_3(self):
        data = np.array([[1, 7, 9, 4, 2], [8, 3, 5, 6, 10]])
        df, ax = task_func(data)
        # Check the dataframe structure and values
        self.assertTrue(isinstance(df, pd.DataFrame))
        self.assertListEqual(
            list(df.columns),
            ["Feature 1", "Feature 2", "Feature 3", "Feature 4", "Feature 5", "Mean"],
        )
        self.assertAlmostEqual(df["Mean"].iloc[0], -0.2)
        self.assertAlmostEqual(df["Mean"].iloc[1], 0.2)
        # Check the histogram plot
        self.assertEqual(ax.get_title(), "Distribution of Means")
        self.assertIsNotNone(ax.patches)  # Check if histogram bars exist
    def test_case_4(self):
        data = np.array(
            [
                [16, 3, 1, 9, 20],
                [2, 12, 13, 8, 17],
                [2, 4, 5, 11, 19],
                [15, 7, 6, 14, 18],
            ]
        )
        df, ax = task_func(data)
        # Check the dataframe structure and values
        self.assertTrue(isinstance(df, pd.DataFrame))
        self.assertListEqual(
            list(df.columns),
            ["Feature 1", "Feature 2", "Feature 3", "Feature 4", "Feature 5", "Mean"],
        )
        # Check the histogram plot
        self.assertEqual(ax.get_title(), "Distribution of Means")
        self.assertIsNotNone(ax.patches)  # Check if histogram bars exist
        # Expected output
        FEATURE_NAMES = [
            "Feature 1",
            "Feature 2",
            "Feature 3",
            "Feature 4",
            "Feature 5",
        ]
        scaler = StandardScaler()
        expected_data = scaler.fit_transform(data)
        np.testing.assert_array_equal(df.loc[:, FEATURE_NAMES].values, expected_data)
    def test_case_5(self):
        data = np.array(
            [
                [1, 2, 3, 4, 5],
                [6, 7, 8, 9, 10],
                [11, 12, 13, 14, 15],
                [16, 17, 18, 19, 20],
                [21, 22, 23, 24, 25],
            ]
        )
        df, ax = task_func(data)
        # Check the dataframe structure and values
        self.assertTrue(isinstance(df, pd.DataFrame))
        self.assertListEqual(
            list(df.columns),
            ["Feature 1", "Feature 2", "Feature 3", "Feature 4", "Feature 5", "Mean"],
        )
        # Check the histogram plot
        self.assertEqual(ax.get_title(), "Distribution of Means")
        self.assertIsNotNone(ax.patches)  # Check if histogram bars exist
        # Expected output
        FEATURE_NAMES = [
            "Feature 1",
            "Feature 2",
            "Feature 3",
            "Feature 4",
            "Feature 5",
        ]
        scaler = StandardScaler()
        expected_data = scaler.fit_transform(data)
        np.testing.assert_array_equal(df.loc[:, FEATURE_NAMES].values, expected_data)