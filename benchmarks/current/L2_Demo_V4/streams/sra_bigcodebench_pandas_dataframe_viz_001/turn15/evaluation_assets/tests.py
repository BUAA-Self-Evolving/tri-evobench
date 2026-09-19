import unittest
# Unit Tests
class TestCases(unittest.TestCase):
    def test_positive_outcomes(self):
        """Test the function with positive goals and penalties."""
        df, _ = task_func(5, 3, rng_seed=42)
        # Check if the DataFrame is not empty and has the correct columns
        self.assertFalse(df.empty)
        self.assertListEqual(list(df.columns), ['Team', 'Goals', 'Penalty Cost'])
    def test_zero_goals_penalties(self):
        """Test the function with zero goals and penalties."""
        df, _ = task_func(0, 0, teams=['Team A'], rng_seed=42)
        # Check that goals and penalty costs are 0
        self.assertTrue((df['Goals'] == 0).all())
        self.assertTrue((df['Penalty Cost'] == 0).all())
    def test_negative_input(self):
        """Ensure negative inputs are treated as positive."""
        df, _ = task_func(-5, -3, rng_seed=42)
        # Check for absence of negative values in results
        self.assertFalse((df['Goals'] < 0).any())
        self.assertFalse((df['Penalty Cost'] < 0).any())
    def test_single_team(self):
        """Test with a single team to ensure correct results."""
        df, _ = task_func(10, 5, teams=['Solo Team'], rng_seed=42)
        # Ensure only one row exists and contains 'Solo Team'
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]['Team'], 'Solo Team')
    def test_custom_penalty_cost(self):
        """Test the function with a custom penalty cost."""
        custom_cost = 500
        df, _ = task_func(5, 3, penalty_cost=custom_cost, rng_seed=42)
        # Validate that the penalty cost calculation uses the custom cost
        self.assertTrue((df['Penalty Cost'] % custom_cost == 0).all() or (df['Penalty Cost'] == 0).all())