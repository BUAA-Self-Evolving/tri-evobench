import unittest
class TestCases(unittest.TestCase):
    def test_dataframe_shape(self):
        """Test that the DataFrame has the correct shape."""
        df = task_func()
        self.assertEqual(df.shape, (SIZE, 2))
    def test_random_range(self):
        """Test that the random numbers fall within the specified range."""
        df = task_func()
        self.assertTrue(df['X'].between(0, RANGE-1).all())
        self.assertTrue(df['Y'].between(0, RANGE-1).all())
    def test_columns_existence(self):
        """Ensure both 'X' and 'Y' columns exist."""
        df = task_func()
        self.assertIn('X', df.columns)
        self.assertIn('Y', df.columns)
    def test_non_empty_dataframe(self):
        """Check that the DataFrame is not empty."""
        df = task_func()
        self.assertFalse(df.empty)
    def test_columns_type(self):
        """Test that 'X' and 'Y' columns are of integer type."""
        df = task_func()
        self.assertTrue(np.issubdtype(df['X'].dtype, np.integer))
        self.assertTrue(np.issubdtype(df['Y'].dtype, np.integer))