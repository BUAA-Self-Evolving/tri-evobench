import unittest
import numpy as np 
class TestCases(unittest.TestCase):
    def setUp(self):
        self.valid_df = pd.DataFrame({
            "group": ["A", "A", "A", "B", "B"],
            "date": pd.to_datetime(["2022-01-02", "2022-01-13", "2022-02-01", "2022-02-23", "2022-03-05"]),
            "value": [10, 20, 16, 31, 56],
        })
    def test_valid_input(self):
        heatmap_fig, pairplot_grid = task_func(self.valid_df)
        self.assertIsInstance(heatmap_fig, plt.Figure)
        self.assertIsInstance(pairplot_grid, sns.axisgrid.PairGrid)
    def test_empty_dataframe(self):
        with self.assertRaises(ValueError):
            task_func(pd.DataFrame())
    def test_missing_columns(self):
        incomplete_df = self.valid_df.drop(columns=['date'])
        with self.assertRaises(ValueError):
            task_func(incomplete_df)
    def test_invalid_date_column(self):
        invalid_df = self.valid_df.copy()
        invalid_df['date'] = "not a date"
        with self.assertRaises(ValueError):
            task_func(invalid_df)
    def test_plot_titles(self):
        heatmap_fig, pairplot_grid = task_func(self.valid_df)
        self.assertEqual(heatmap_fig.axes[0].get_title(), 'Correlation Matrix')
    
    def test_value_consistency(self):
        df = self.valid_df.copy()
        df['date'] = df['date'].apply(lambda x: x.toordinal())
        df_numeric = df.drop(columns=['group'])
        heatmap_fig, _ = task_func(self.valid_df)
        # Retrieve the correlation matrix data from the heatmap and reshape it
        heatmap_data = heatmap_fig.axes[0].collections[0].get_array().data
        heatmap_data_reshaped = heatmap_data.reshape(df_numeric.corr().shape)
        expected_corr_matrix = df_numeric.corr().values
        # Compare the reshaped data in the heatmap with the expected correlation matrix
        np.testing.assert_array_almost_equal(heatmap_data_reshaped, expected_corr_matrix)