import unittest
import pandas as pd
class TestCases(unittest.TestCase):
    """Test cases for the task_func function."""
    def test_case_1(self):
        data = {
            "Age": [25, 30, 35, 40, 45],
            "Height": [160, 155, 170, 165, 150],
            "Weight": [60, 65, 70, 75, 80],
        }
        df = pd.DataFrame(data)
        result, ax = task_func(df, 28, 165)
        self.assertTrue(isinstance(result, pd.DataFrame))
        self.assertTrue("Cluster" in result.columns)
        self.assertListEqual(result["Cluster"].tolist(), [0, 0])
        self.assertTrue(max(result.loc[:, "Cluster"]) < 3)
        self.assertEqual(len(result), 2)
        self.assertIsNone(ax)
    def test_case_2(self):
        data = {
            "Age": [20, 25, 30, 35, 40],
            "Height": [150, 155, 160, 165, 170],
            "Weight": [55, 60, 65, 70, 75],
        }
        df = pd.DataFrame(data)
        result, ax = task_func(df, 30, 160)
        self.assertTrue(isinstance(result, pd.DataFrame))
        self.assertTrue("Cluster" in result.columns or len(result) < 3)
        self.assertEqual(len(result), 0)
        self.assertIsNone(ax)
    def test_case_3(self):
        data = {
            "Age": [29, 30, 35, 40, 75],
            "Height": [140, 155, 170, 165, 210],
            "Weight": [60, 65, 70, 75, 70],
        }
        df = pd.DataFrame(data)
        result, ax = task_func(df, 28, 220)
        self.assertTrue(isinstance(result, pd.DataFrame))
        self.assertTrue("Cluster" in result.columns or len(result) < 3)
        self.assertEqual(len(result), 5)
        self.assertEqual(ax.get_xlabel(), "Age")
        self.assertEqual(ax.get_ylabel(), "Height")
        self.assertEqual(ax.get_title(), "KMeans Clustering based on Age and Height")
    def test_case_4(self):
        data = {
            "Age": [25, 30, 35, 40, 45],
            "Height": [160, 155, 170, 165, 150],
            "Weight": [60, 65, 70, 75, 80],
        }
        df = pd.DataFrame(data)
        result, ax = task_func(df, 28, 180)
        self.assertTrue(isinstance(result, pd.DataFrame))
        self.assertTrue("Cluster" in result.columns)
        self.assertTrue(max(result.loc[:, "Cluster"]) < 3)
        self.assertEqual(len(result), 4)
    def test_case_5(self):
        data = {
            "Age": [25, 30, 35, 40, 45],
            "Height": [160, 155, 170, 165, 150],
            "Weight": [60, 65, 70, 75, 80],
        }
        df = pd.DataFrame(data)
        result, ax = task_func(df, 24, 165)
        self.assertTrue(isinstance(result, pd.DataFrame))
        self.assertTrue("Cluster" in result.columns)
        self.assertTrue(max(result.loc[:, "Cluster"]) < 3)
        self.assertEqual(len(result), 3)
