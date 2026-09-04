import unittest
import pandas as pd
class TestCases(unittest.TestCase):
    def test_normal_case(self):
        # Normal case with valid DataFrame
        df = pd.DataFrame([['2021-01-01', [8, 10, 12]], ['2021-01-02', [7, 9, 11]]], columns=['Date', 'Value'])
        result= task_func(df)
        self.assertEqual(result.shape, (2, 4))  # Checking if the DataFrame has the correct shape
        plt.close()
    def test_varying_length_lists(self):
        # DataFrame where 'Value' contains lists of varying lengths
        df = pd.DataFrame([['2021-01-01', [8, 10]], ['2021-01-02', [7, 9, 11]]], columns=['Date', 'Value'])
        result = task_func(df)
        self.assertEqual(result.shape, (2, 4))  # The function should handle varying lengths
        plt.close()
    def test_varying_length_list_2(self):
        df = pd.DataFrame([['2021-01-01', [8, 10, 12]], ['2021-01-02', [7, 9, 11]]], columns=['Date', 'Value'])
        result = task_func(df)
        self.assertEqual(result.empty, False)  
        plt.close()
    def test_missing_columns(self):
        # DataFrame missing 'Value' column
        df = pd.DataFrame([['2021-01-01'], ['2021-01-02']], columns=['Date'])
        with self.assertRaises(KeyError):
            task_func(df)  # Expecting a KeyError due to missing 'Value' column
        plt.close()
    def test_empty(self):
        df = pd.DataFrame()
        with self.assertRaises(KeyError):
            task_func(df)  
        plt.close()
    def test_plot_attributes(self):
        df = pd.DataFrame([['2021-01-01', [8, 10, 12]], ['2021-01-02', [7, 9, 11]]], columns=['Date', 'Value'])
        _, ax = task_func(df, True)
        self.assertEqual(ax.get_title(), 'Scaled Values Over Time')
        self.assertEqual(ax.get_xlabel(), 'Date')
        self.assertEqual(ax.get_ylabel(), 'Scaled Value')
        plt.close()
    def test_plot_point(self):
        df = pd.DataFrame([['2021-01-01', [8, 10, 12]], ['2021-01-02', [7, 9, 11]]], columns=['Date', 'Value'])
        result, ax = task_func(df, True)
        list_result = []
        for column in result:
            if column != "Date":
                columnSeriesObj = result[column]
                list_result.extend(columnSeriesObj.values)
        bar_heights = [rect.get_height() for rect in ax.patches]
        self.assertListEqual(bar_heights, list_result)
        plt.close()