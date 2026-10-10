import unittest
import tempfile
import shutil
import os
import configparser
class TestCases(unittest.TestCase):
    def setUp(self):
        # Setup a temporary directory for the configuration files and another for the archive output
        self.test_data_dir = tempfile.mkdtemp()
        self.archive_dir = tempfile.mkdtemp()
        # Example valid configuration file setup
        self.valid_config_path = os.path.join(self.test_data_dir, "valid_config.ini")
        config = configparser.ConfigParser()
        config['Project'] = {'directory': self.test_data_dir}
        with open(self.valid_config_path, 'w') as configfile:
            config.write(configfile)
        # Invalid directory config
        self.invalid_config_path = os.path.join(self.test_data_dir, "invalid_config.ini")
        config['Project'] = {'directory': '/path/to/nonexistent/directory'}
        with open(self.invalid_config_path, 'w') as configfile:
            config.write(configfile)
    def tearDown(self):
        # Remove temporary directories after each test
        shutil.rmtree(self.test_data_dir)
        shutil.rmtree(self.archive_dir)
    def test_valid_project_directory(self):
        # Testing with a valid project directory
        result = task_func(self.valid_config_path, self.archive_dir)
        self.assertTrue(result)
    def test_invalid_project_directory(self):
        # Testing with a non-existent project directory
        with self.assertRaises(FileNotFoundError):
            task_func(self.invalid_config_path, self.archive_dir)
    def test_archive_creation(self):
        # Run the function to create the archive
        task_func(self.valid_config_path, self.archive_dir)
        archive_file = os.path.join(self.archive_dir, os.path.basename(self.test_data_dir) + '.zip')
        self.assertTrue(os.path.isfile(archive_file))
    def test_archive_content(self):
        # Adding a sample file to the project directory to check archive contents later
        sample_file_path = os.path.join(self.test_data_dir, "sample_file.txt")
        with open(sample_file_path, 'w') as f:
            f.write("Hello, world!")
        task_func(self.valid_config_path, self.archive_dir)
        archive_file = os.path.join(self.archive_dir, os.path.basename(self.test_data_dir) + '.zip')
        content = os.popen(f"unzip -l {archive_file}").read()
        self.assertIn("sample_file.txt", content)
