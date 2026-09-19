import unittest
class TestCases(unittest.TestCase):
    def test_dataframe_shape(self):
        """Test if the DataFrame has the correct shape."""
        df = task_func()
        self.assertEqual(df.shape, (12, 5))  # 12 months and 5 products
    def test_dataframe_columns(self):
        """Test if the DataFrame has the correct column names."""
        df = task_func()
        expected_columns = PRODUCTS
        self.assertListEqual(list(df.columns), expected_columns)
    def test_dataframe_index(self):
        """Test if the DataFrame has the correct index."""
        df = task_func()
        expected_index = MONTHS
        self.assertListEqual(list(df.index), expected_index)
    def test_sales_range(self):
        """Test if sales figures are within the expected range."""
        df = task_func()
        self.assertTrue((df >= 100).all().all() and (df <= 1000).all().all())
    def test_returns_dataframe(self):
        """Test if the function returns a pandas DataFrame."""
        df = task_func()
        self.assertIsInstance(df, pd.DataFrame)