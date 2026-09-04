import unittest
import pandas as pd
import matplotlib.pyplot as plt
class TestCases(unittest.TestCase):
    def _validate_plot(self, ax):
        self.assertEqual(ax.get_title(), "Grade Distribution")
        self.assertEqual(ax.get_xlabel(), "Grade")
        self.assertEqual(ax.get_ylabel(), "Number of Students")
    def _test_helper(self, grades, expected_counts):
        expected_df = pd.DataFrame(
            {"Count": expected_counts}, index=["A", "B", "C", "D", "F"]
        )
        expected_df.index.name = "Grade"
        report_df, ax = task_func(grades)
        pd.testing.assert_frame_equal(report_df, expected_df)
        self._validate_plot(ax)
    def test_case_1(self):
        # Test with a mix of grades
        self._test_helper(
            ["A", "B", "B", "C", "A", "D", "F", "B", "A", "C"], [3, 3, 2, 1, 1]
        )
    def test_case_2(self):
        # Test with only one type of grade
        self._test_helper(["A", "A", "A", "A", "A"], [5, 0, 0, 0, 0])
    def test_case_3(self):
        # Test with an empty list of grades
        with self.assertRaises(Exception):
            task_func([], [0, 0, 0, 0, 0])
    def test_case_4(self):
        # Test correctly ignoring invalid grades
        self._test_helper(["A", "X", "Y", "Z"], [1, 0, 0, 0, 0])
    def test_case_5(self):
        # Test custom grades
        grades = ["A", "C", "G", "G"]
        expected_counts = [1, 0, 1, 0, 0, 2]
        possible_grades = ["A", "B", "C", "D", "F", "G"]
        expected_df = pd.DataFrame(
            {"Count": expected_counts},
            index=[*dict.fromkeys(g.upper() for g in possible_grades)],
        )
        expected_df.index.name = "Grade"
        report_df, ax = task_func(grades, possible_grades=possible_grades)
        pd.testing.assert_frame_equal(report_df, expected_df)
        self._validate_plot(ax)
    def test_case_6(self):
        # Test case insensitivity
        self._test_helper(["a", "b", "C"], [1, 1, 1, 0, 0])
    def test_case_7(self):
        # Test whitespace sensitivity
        self._test_helper(["A ", "b", " C"], [0, 1, 0, 0, 0])
    def tearDown(self):
        plt.close("all")