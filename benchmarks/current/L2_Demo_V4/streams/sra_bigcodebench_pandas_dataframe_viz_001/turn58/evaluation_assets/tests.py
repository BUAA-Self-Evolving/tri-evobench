import unittest
class TestCases(unittest.TestCase):
    def setUp(self):
        self.df = pd.DataFrame({
            "group": ["A", "A", "A", "B", "B"],
            "date": pd.to_datetime(["2022-01-02", "2022-01-13", "2022-02-01", "2022-02-23", "2022-03-05"]),
            "value": [10, 20, 16, 31, 56],
        })
    def test_return_type(self):
        ax = task_func(self.df)
        self.assertIsInstance(ax, plt.Axes)
    def test_invalid_dataframe(self):
        with self.assertRaises(ValueError):
            task_func(pd.DataFrame({'a': [1, 2], 'b': [3, 4]}))
    def test_custom_groups(self):
        custom_groups = ['A', 'B']
        ax = task_func(self.df, groups=custom_groups)
        # Check if only the custom groups are plotted
        plotted_groups = set(self.df[self.df['group'].isin(custom_groups)]['group'].unique())
        self.assertEqual(len(plotted_groups), len(custom_groups))
    def test_plot_labels(self):
        ax = task_func(self.df)
        self.assertEqual(ax.get_xlabel(), 'Date (ordinal)')
        self.assertEqual(ax.get_ylabel(), 'Value')
        self.assertEqual(ax.get_title(), 'Scatterplot of Values for Each Group Over Time')