import pandas as pd
import numpy as np
import unittest
import os
from sklearn.datasets import make_blobs
class TestCases(unittest.TestCase):
    """Tests for task_func."""
    def setUp(self) -> None:
        os.environ["LOKY_MAX_CPU_COUNT"] = "2"
    def test_random_data_size_100(self):
        """Test with random data of size 100 and default number of clusters"""
        np.random.seed(42)
        s1 = pd.Series(np.random.rand(100), name="feature1")
        np.random.seed(0)
        s2 = pd.Series(np.random.rand(100), name="feature2")
        labels, ax = task_func(s1, s2)
        # Check if labels are ndarray
        self.assertIsInstance(labels, np.ndarray)
        # Check the plot's title
        self.assertEqual(ax.get_title(), "K-Means Clustering")
    def test_random_data_custom_clusters(self):
        """Test with random data of size 100 and custom number of clusters"""
        np.random.seed(42)
        s1 = pd.Series(np.random.rand(100), name="feature1")
        np.random.seed(0)
        s2 = pd.Series(np.random.rand(100), name="feature2")
        labels, ax = task_func(s1, s2, n_clusters=5)
        # Check if labels are ndarray
        self.assertIsInstance(labels, np.ndarray)
        self.assertEqual(len(set(labels)), 5)
        # Check the plot's title
        self.assertEqual(ax.get_title(), "K-Means Clustering")
    def test_invalid_input_non_series(self):
        """Test with invalid input types (non-Series)"""
        with self.assertRaises(ValueError):
            task_func([1, 2, 3], pd.Series([4, 5, 6]))
    def test_invalid_input_mismatched_length(self):
        """Test with mismatched length of Series"""
        s1 = pd.Series([1, 2, 3], name="feature1")
        s2 = pd.Series([4, 5], name="feature2")
        with self.assertRaises(ValueError):
            task_func(s1, s2)
    def test_custom_clusters_with_synthetic_data(self):
        """Test with synthetic data and custom number of clusters using make_blobs"""
        # Generate synthetic data with 2 distinct clusters
        X, _ = make_blobs(n_samples=100, centers=2, random_state=42)
        # Convert to pandas Series
        s1 = pd.Series(X[:, 0], name="feature1")
        s2 = pd.Series(X[:, 1], name="feature2")
        # Run the clustering function
        labels, ax = task_func(s1, s2, n_clusters=2)
        # Check if labels are ndarray
        self.assertIsInstance(labels, np.ndarray)
        # Check the number of unique labels (should be 2 for 2 clusters)
        self.assertEqual(len(set(labels)), 2)
        # Check the plot's title
        self.assertEqual(ax.get_title(), "K-Means Clustering")
    def tearDown(self):
        plt.clf()