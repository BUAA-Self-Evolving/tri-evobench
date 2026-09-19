import unittest
from unittest.mock import patch
class TestCases(unittest.TestCase):
    @patch('random.randint')
    def test_case_1(self, mock_randint):
        """Test with diverse values and the default range to ensure percentile calculation."""
        mock_randint.return_value = 50  # Mocking random.randint to always return 50
        T1 = (('13', '17', '18', '21', '32'), ('07', '11', '13', '14', '28'), ('01', '05', '06', '08', '15', '16'))
        p25, p50, p75 = task_func(T1)
        self.assertEqual(p25, 50)
        self.assertEqual(p50, 50)
        self.assertEqual(p75, 50)
    @patch('random.randint')
    def test_case_2(self, mock_randint):
        """Check consistency when the total number of elements are small but repeated."""
        mock_randint.return_value = 30  # Consistent lower value for a different perspective
        T1 = (('10',), ('10', '10', '10'))
        p25, p50, p75 = task_func(T1)
        self.assertEqual(p25, 30)
        self.assertEqual(p50, 30)
        self.assertEqual(p75, 30)
    @patch('random.randint')
    def test_case_3(self, mock_randint):
        """Ensure that percentile calculations are consistent for mixed low and medium values."""
        mock_randint.return_value = 75  # Higher consistent value
        T1 = (('5', '5', '5', '5'), ('10', '15'), ('1', '2', '3', '4', '5'))
        p25, p50, p75 = task_func(T1)
        self.assertEqual(p25, 75)
        self.assertEqual(p50, 75)
        self.assertEqual(p75, 75)
    @patch('random.randint')
    def test_case_4(self, mock_randint):
        """Tests percentile values for a simple large-value case."""
        mock_randint.return_value = 10  # Low consistent value to see impact on percentiles
        T1 = (('50',), ('25', '25'))
        p25, p50, p75 = task_func(T1)
        self.assertEqual(p25, 10)
        self.assertEqual(p50, 10)
        self.assertEqual(p75, 10)
    @patch('random.randint')
    def test_case_5(self, mock_randint):
        """Test with an extreme case where all random numbers are the same, ensuring no variability."""
        mock_randint.return_value = 90  # High consistent value
        T1 = (('1', '1', '1', '1', '1', '1', '1', '1', '1', '1'), ('10', '10'))
        p25, p50, p75 = task_func(T1)
        self.assertEqual(p25, 90)
        self.assertEqual(p50, 90)
        self.assertEqual(p75, 90)
