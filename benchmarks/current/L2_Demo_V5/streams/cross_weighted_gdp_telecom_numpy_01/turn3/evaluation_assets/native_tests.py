import unittest
import numpy as np
from functools import reduce
class TestCases(unittest.TestCase):

    def test_case_1(self):
        # Basic test case with positive and negative numbers
        list_of_pairs = [('Fruits', 5), ('Vegetables', 9), ('Dairy', -1), ('Bakery', -2), ('Meat', 4)]
        expected_output = np.array([360])
        actual_output = task_func(list_of_pairs)
        print(actual_output, expected_output)
        self.assertTrue(np.array_equal(actual_output, expected_output))

    def test_case_2(self):
        # Test case with all positive numbers
        list_of_pairs = [('A', 2), ('B', 3), ('C', 4)]
        expected_output = np.array([24])
        actual_output = task_func(list_of_pairs)
        self.assertTrue(np.array_equal(actual_output, expected_output))

    def test_case_3(self):
        # Test case with all negative numbers
        list_of_pairs = [('A', -2), ('B', -3), ('C', -4)]
        expected_output = np.array([-24])
        actual_output = task_func(list_of_pairs)
        self.assertTrue(np.array_equal(actual_output, expected_output))

    def test_case_4(self):
        # Test case with a single tuple
        list_of_pairs = [('A', 10)]
        expected_output = np.array([10])
        actual_output = task_func(list_of_pairs)
        self.assertTrue(np.array_equal(actual_output, expected_output))

    def test_case_5(self):
        # Test case with zeros
        list_of_pairs = [('A', 0), ('B', 5), ('C', 10)]
        expected_output = np.array([0])
        actual_output = task_func(list_of_pairs)
        self.assertTrue(np.array_equal(actual_output, expected_output))
