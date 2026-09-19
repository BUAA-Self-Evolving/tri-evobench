import unittest

class TestCases(unittest.TestCase):
    def setUp(self):
        # Common setup for all tests: explicitly define the list of letters
        self.letters = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm', 'n', 'o', 'p', 'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z']
    def test_case_1(self):
        # Test if the function returns a dictionary
        mean_dict = task_func(self.letters)
        self.assertIsInstance(mean_dict, dict)
    def test_case_2(self):
        # Test if the dictionary contains all letters of the alphabet
        mean_dict = task_func(self.letters)
        self.assertTrue(all(letter in mean_dict for letter in self.letters))

    def test_case_3(self):
        # Test if the values in the dictionary are floats (means of lists of integers)
        mean_dict = task_func(self.letters)
        self.assertTrue(all(isinstance(val, float) for val in mean_dict.values()))
    def test_case_4(self):
        # Test if the mean values are reasonable given the range of random integers (0-100)
        mean_dict = task_func(self.letters)
        self.assertTrue(all(0 <= val <= 100 for val in mean_dict.values()))
    def test_case_5(self):
        # Test if the dictionary has 26 keys (one for each letter of the alphabet)
        mean_dict = task_func(self.letters)
        self.assertEqual(len(mean_dict), 26)
