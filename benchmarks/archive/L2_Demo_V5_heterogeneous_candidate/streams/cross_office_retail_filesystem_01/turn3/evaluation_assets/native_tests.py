import unittest
import tempfile
class TestCases(unittest.TestCase):
    def test_non_empty_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create sample files
            with open(os.path.join(temp_dir, 'file1.txt'), 'w') as f:
                f.write('Hello')
            with open(os.path.join(temp_dir, 'file2.txt'), 'w') as f:
                f.write('World')
            # Run the function
            csv_path = task_func(temp_dir)
            # Verify CSV file creation and contents
            self.assertTrue(os.path.exists(csv_path), 'CSV file not created')
            with open(csv_path, 'r') as csvfile:
                reader = csv.reader(csvfile)
                rows = list(reader)
                self.assertEqual(len(rows), 3, 'Incorrect number of rows in CSV')
                self.assertEqual(rows[1][1], '5', 'Incorrect file size for file1.txt')
                self.assertEqual(rows[2][1], '5', 'Incorrect file size for file2.txt')
    def test_empty_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = task_func(temp_dir)
            self.assertTrue(os.path.exists(csv_path), 'CSV file not created in empty directory')
            with open(csv_path, 'r') as csvfile:
                reader = csv.reader(csvfile)
                rows = list(reader)
                self.assertEqual(len(rows), 1, 'CSV file should only contain headers in empty directory')
    def test_nested_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create sample files in nested directories
            os.makedirs(os.path.join(temp_dir, 'subdir1'))
            os.makedirs(os.path.join(temp_dir, 'subdir2'))
            with open(os.path.join(temp_dir, 'subdir1', 'file1.txt'), 'w') as f:
                f.write('Hello')
            with open(os.path.join(temp_dir, 'subdir2', 'file2.txt'), 'w') as f:
                f.write('World')
            # Run the function
            csv_path = task_func(temp_dir)
            # Verify CSV file creation and contents
            self.assertTrue(os.path.exists(csv_path), 'CSV file not created for nested directories')
            with open(csv_path, 'r') as csvfile:
                reader = csv.reader(csvfile)
                rows = list(reader)
                self.assertEqual(len(rows), 3, 'Incorrect number of rows in CSV for nested directories')
                self.assertEqual(rows[1][1], '5', 'Incorrect file size for subdir1/file1.txt')
                self.assertEqual(rows[2][1], '5', 'Incorrect file size for subdir2/file2.txt')

    def test_single_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create sample files
            with open(os.path.join(temp_dir, 'file1.txt'), 'w') as f:
                f.write('Hellooooooooooo')
            csv_path = task_func(temp_dir)
            self.assertTrue(os.path.exists(csv_path), 'CSV file not created')
    def test_large_number_of_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create a large number of files
            for i in range(100):
                with open(os.path.join(temp_dir, f'file{i}.txt'), 'w') as f:
                    f.write(str(i))

            csv_path = task_func(temp_dir)
            self.assertTrue(os.path.exists(csv_path), 'CSV file not created for large number of files')
            with open(csv_path, 'r') as csvfile:
                reader = csv.reader(csvfile)
                rows = list(reader)
                self.assertEqual(len(rows), 101, 'Incorrect number of rows for large number of files')
