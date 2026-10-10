import unittest
import tempfile
import os
import subprocess
import glob
import shutil
class TestCases(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.temp_backup_dir = tempfile.mkdtemp()

        # Create some log files and some non-log files
        for i in range(5):
            with open(os.path.join(self.temp_dir, f"file_{i}.log"), "w") as f:
                f.write(f"Mock log content for file_{i}")
            with open(os.path.join(self.temp_dir, f"file_{i}.txt"), "w") as f:
                f.write(f"Mock content for file_{i}.txt")
    def tearDown(self):
        shutil.rmtree(self.temp_dir)
        shutil.rmtree(self.temp_backup_dir)
    def test_backup_creation_and_log_file_deletion(self):
        # Test the creation of the backup file and deletion of original log files.
        backup_path = task_func(self.temp_dir, self.temp_backup_dir)
        self.assertTrue(os.path.exists(backup_path))
        self.assertEqual(backup_path, os.path.join(self.temp_backup_dir, 'logs_backup.tar.gz'))
        self.assertFalse(any(file.endswith('.log') for file in os.listdir(self.temp_dir)))
    def test_no_log_files_to_backup(self):
        # Test behavior when no log files are present in the directory.
        empty_dir = tempfile.mkdtemp()
        result = task_func(empty_dir, self.temp_backup_dir)
        self.assertEqual(result, "No logs found to backup")
        shutil.rmtree(empty_dir)
    def test_non_log_files_remain(self):
        # Ensure that non-log files are not deleted or included in the backup.
        backup_path = task_func(self.temp_dir, self.temp_backup_dir)
        self.assertEqual(len(glob.glob(os.path.join(self.temp_dir, '*.txt'))), 5)  # Check only non-log files remain
    def test_handle_non_existing_directory(self):
        # Verify that a FileNotFoundError is raised for a non-existing source directory.
        with self.assertRaises(FileNotFoundError):
            task_func('/non/existing/directory', self.temp_backup_dir)
