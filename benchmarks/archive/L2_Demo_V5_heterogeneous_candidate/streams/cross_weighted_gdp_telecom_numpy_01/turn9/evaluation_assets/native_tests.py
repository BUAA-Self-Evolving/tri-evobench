import unittest
class TestCases(unittest.TestCase):
    """Test cases for the task_func function."""
    def test_case_1(self):
        data = np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9]])
        self._validate_function(data)
    def test_case_2(self):
        data = np.array([[0, 0, 1], [0, 1, 0], [1, 0, 0]])
        self._validate_function(data)
    def test_case_3(self):
        data = np.array([[3, 5, 7, 1000], [200, 5, 7, 1], [1, 9, 14, 700]])
        self._validate_function(data)
    def test_case_4(self):
        data = np.array(
            [
                [1, 2, 3, 4, 5, 4, 3, 2, 1],
            ]
        )
        self._validate_function(data)
    def test_case_5(self):
        data = np.array([[1], [1], [1]])
        self._validate_function(data)
    def _validate_function(self, data):
        indices, ax = task_func(data)
        self.assertIsInstance(indices, list)
        lines = ax.get_lines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0].get_color(), "r")
        self.assertEqual(lines[0].get_label(), "Means")
        self.assertEqual(lines[1].get_color(), "b")
        self.assertEqual(lines[1].get_label(), "Significant Means")
        self.assertEqual(lines[2].get_color(), "g")
        self.assertEqual(lines[2].get_label(), "Population Mean")
