import unittest
from unittest.mock import patch
import io
class TestCases(unittest.TestCase):
    """Tests for the function."""
    def test_empty_list(self):
        """
        Test the function with an empty list. Expects ValueError.
        """
        with self.assertRaises(ValueError):
            task_func([])
    def test_uniform_distribution(self):
        """
        Test the function with a uniform distribution of predefined categories.
        Expects no printed warning about non-uniform distribution.
        """
        data = ["A", "B", "C", "D", "E"] * 2
        with patch("sys.stdout", new=io.StringIO()) as fake_output:
            task_func(data)
        self.assertNotIn(
            "The distribution of predefined categories is not uniform.",
            fake_output.getvalue(),
        )
    def test_non_uniform_distribution(self):
        """
        Test the function with a non-uniform distribution of predefined categories.
        Expects a printed warning about non-uniform distribution.
        """
        data = ["A", "A", "B", "C", "D", "E"]
        with patch("sys.stdout", new=io.StringIO()) as fake_output:
            task_func(data)
        self.assertIn(
            "The distribution of predefined categories is not uniform.",
            fake_output.getvalue(),
        )
    def test_extra_categories(self):
        """
        Test the function with extra categories not in the predefined list.
        Expects extra categories to be included in the histogram.
        """
        data = ["A", "B", "C", "D", "E", "F", "G"]
        ax = task_func(data)
        self.assertIn("F", [tick.get_text() for tick in ax.get_xticklabels()])
        self.assertIn("G", [tick.get_text() for tick in ax.get_xticklabels()])
    def test_no_extra_categories(self):
        """
        Test the function with no extra categories.
        Expects only predefined categories to be included in the histogram.
        """
        data = ["A", "B", "C", "D", "E"]
        ax = task_func(data)
        for extra_cat in ["F", "G"]:
            self.assertNotIn(
                extra_cat, [tick.get_text() for tick in ax.get_xticklabels()]
            )
    def tearDown(self):
        plt.clf()