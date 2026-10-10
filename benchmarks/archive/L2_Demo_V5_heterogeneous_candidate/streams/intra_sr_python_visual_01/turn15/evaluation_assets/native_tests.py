import unittest
import json
import os
import re
EMAIL_REGEX = r"(^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$)"
class TestCases(unittest.TestCase):
    def setUp(self):
        # Creating a dummy JSON file
        self.filepath = '/tmp/test_data.json'
        self.valid_data = {
            "name": "John Doe",
            "age": 30,
            "email": "john.doe@example.com"
        }
        self.invalid_email_data = {
            "name": "John Doe",
            "age": 30,
            "email": "johndoe@example"
        }
        with open(self.filepath, 'w') as file:
            json.dump(self.valid_data, file)

    def tearDown(self):
        # Remove the dummy JSON file after the test
        os.remove(self.filepath)
    def test_case_valid_json(self):
        # Test with valid JSON data
        result = task_func(self.filepath, 'name')
        self.assertEqual(result, "John Doe")

    def test_case_invalid_email_format(self):
        # Overwrite with invalid email format data and test
        with open(self.filepath, 'w') as file:
            json.dump(self.invalid_email_data, file)
        with self.assertRaises(ValueError):
            task_func(self.filepath, 'email')

    def test_case_missing_attribute(self):
        # Test with JSON missing a required attribute by removing 'age'
        modified_data = self.valid_data.copy()
        del modified_data['age']
        with open(self.filepath, 'w') as file:
            json.dump(modified_data, file)
        with self.assertRaises(ValueError):
            task_func(self.filepath, 'age')

    def test_case_retrieve_age(self):
        # Test retrieving age from valid JSON
        result = task_func(self.filepath, 'age')
        self.assertEqual(result, 30)
    def test_case_non_existent_file(self):
        # Test with non-existent file path
        with self.assertRaises(ValueError):
            task_func('/tmp/non_existent.json', 'name')
