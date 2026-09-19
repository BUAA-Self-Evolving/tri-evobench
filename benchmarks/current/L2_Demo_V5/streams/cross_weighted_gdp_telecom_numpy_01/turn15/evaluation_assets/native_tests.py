import unittest
class TestCases(unittest.TestCase):
    """Test cases for the task_func function."""
    def test_case_1(self):
        np.random.seed(42)
        mu = 0
        sigma = 1
        num_samples = 1000
        fig = task_func(mu, sigma, num_samples)
        ax = fig.gca()
        self.assertEqual(ax.get_title(), "Normal Distribution")
        self.assertTrue(len(ax.patches) > 0)
        self.assertTrue(len(ax.lines) > 0)
    def test_case_2(self):
        np.random.seed(42)
        mu = 5
        sigma = 2
        num_samples = 1000
        fig = task_func(mu, sigma, num_samples)
        ax = fig.gca()
        self.assertEqual(ax.get_title(), "Normal Distribution")
        self.assertTrue(len(ax.patches) > 0)
        self.assertTrue(len(ax.lines) > 0)
    def test_case_3(self):
        np.random.seed(42)
        mu = 0
        sigma = 1
        num_samples = 10
        fig = task_func(mu, sigma, num_samples)
        ax = fig.gca()
        self.assertEqual(ax.get_title(), "Normal Distribution")
        self.assertTrue(len(ax.patches) > 0)
        self.assertTrue(len(ax.lines) > 0)
    def test_case_4(self):
        np.random.seed(42)
        mu = 0
        sigma = 1
        num_samples = 10
        fig = task_func(mu, sigma, num_samples)
        ax = fig.gca()
        self.assertEqual(ax.get_title(), "Normal Distribution")
        self.assertTrue(len(ax.patches) > 0)
        self.assertTrue(len(ax.lines) > 0)
    def test_case_5(self):
        np.random.seed(42)
        mu = 0
        sigma = 1
        num_samples = 10
        fig = task_func(mu, sigma, num_samples)
        ax = fig.gca()
        self.assertEqual(ax.get_title(), "Normal Distribution")
        self.assertTrue(len(ax.patches) > 0)
        self.assertTrue(len(ax.lines) > 0)
