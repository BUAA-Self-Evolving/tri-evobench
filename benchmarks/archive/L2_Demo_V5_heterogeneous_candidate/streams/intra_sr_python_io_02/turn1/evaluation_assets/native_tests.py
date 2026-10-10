import unittest
import tempfile
import os
import shutil
class TestCases(unittest.TestCase):
    def setUp(self):
        # Setup temporary directories for the test
        self.directory = tempfile.mkdtemp()
        self.backup_directory = tempfile.mkdtemp()
    def tearDown(self):
        # Only attempt to remove the directories if they still exist
        if os.path.exists(self.directory):
            shutil.rmtree(self.directory)
        if os.path.exists(self.backup_directory):
            shutil.rmtree(self.backup_directory)
    def test_backup_directory_creation(self):
        """ Test that the backup directory is created if it does not exist. """
        shutil.rmtree(self.backup_directory)  # Ensure the backup directory does not exist
        task_func(self.directory, self.backup_directory)
        self.assertTrue(os.path.exists(self.backup_directory))
    def test_file_copying(self):
        """ Test that files are correctly copied to the backup directory. """
        # Create a test JSON file in the source directory
        test_file = os.path.join(self.directory, 'test1.json')
        with open(test_file, 'w') as f:
            f.write('{"test": "data"}')
        task_func(self.directory, self.backup_directory)
        copied_file = os.path.join(self.backup_directory, 'test1.json')
        self.assertTrue(os.path.exists(copied_file))
    def test_json_file_selection(self):
        """ Test that only JSON files are selected for copying. """
        # Create both JSON and non-JSON files
        json_file = os.path.join(self.directory, 'test1.json')
        txt_file = os.path.join(self.directory, 'test2.txt')
        with open(json_file, 'w') as f:
            f.write('{"test": "data"}')
        with open(txt_file, 'w') as f:
            f.write("some text")
        result = task_func(self.directory, self.backup_directory)
        self.assertEqual(len(result), 1)  # Only one JSON file should be copied
        self.assertTrue('test1.json' in result[0])
    def test_handling_nonexistent_directory(self):
        """ Test the function's behavior with a non-existent source directory. """
        shutil.rmtree(self.directory)  # Remove the source directory to simulate non-existence
        with self.assertRaises(FileNotFoundError):
            task_func(self.directory, self.backup_directory)  # This should raise FileNotFoundError
    def test_return_type(self):
        """ Test that the function returns a list. """
        result = task_func(self.directory, self.backup_directory)
        self.assertIsInstance(result, list)
