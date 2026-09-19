import unittest
import pandas as pd
class TestCases(unittest.TestCase):
    def setUp(self):
        self.data = pd.DataFrame({
            'Column1': [14, 1, 7, 10, 5],
            'Column2': [25, 22, 8, 15, 11]
        })
    def test_regression_results(self):
        result, _ = task_func(self.data, 'Column1', 'Column2')
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 5)
    def test_invalid_columns(self):
        with self.assertRaises(ValueError):
            task_func(self.data, 'Invalid1', 'Column2')
    def test_plot_axes(self):
        _, ax = task_func(self.data, 'Column1', 'Column2')
        self.assertEqual(len(ax.lines), 2)  # Original data and fitted line
    def test_empty_dataframe(self):
        with self.assertRaises(ValueError):
            task_func(pd.DataFrame(), 'Column1', 'Column2')
    def test_single_point_regression(self):
        single_point_data = pd.DataFrame({'Column1': [1], 'Column2': [2]})
        result, ax = task_func(single_point_data, 'Column1', 'Column2')
        # self.assertEqual(result[0], np.nan)
        self.assertEqual(result[2], 0)  # Slope should be 0 for single point

    def test_return_values(self):
        result, ax = task_func(self.data, 'Column1', 'Column2')
        # print(result)
        # with open('df_contents.txt', 'w') as file:
        #     file.write(str(result))
        expect = (0.3456790123456789, 13.641975308641975, 0.23699046752221187, 0.7011032163730078, 0.8181438416490141)
        for res, exp in zip(result, expect):
            self.assertAlmostEqual(res, exp, places=7)
