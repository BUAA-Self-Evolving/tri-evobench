import unittest
from unittest.mock import patch, mock_open
class TestCases(unittest.TestCase):

    @patch("os.path.isfile", return_value=True)
    @patch("subprocess.call", return_value=0)
    @patch("builtins.open", new_callable=mock_open, read_data="{}")
    def test_default_values_successful_script(self, mock_file, mock_subprocess, mock_os):
        """Test the function with default parameters and successful execution"""
        result = task_func()
        self.assertIn('start_time', result)
        self.assertIn('end_time', result)
        self.assertEqual(result['exit_status'], 0)
    @patch("os.path.isfile", return_value=False)
    def test_script_does_not_exist(self, mock_os):
        """Test the function raising FileNotFoundError when the script file does not exist"""
        with self.assertRaises(FileNotFoundError):
            task_func()
    @patch("os.path.isfile", return_value=True)
    @patch("subprocess.call", side_effect=Exception("Script failed"))
    def test_script_execution_failure(self, mock_subprocess, mock_os):
        """Test the function raising RuntimeError on script execution failure"""
        with self.assertRaises(RuntimeError):
            task_func()
    @patch("os.path.isfile", return_value=True)
    @patch("subprocess.call", return_value=0)
    @patch("builtins.open", new_callable=mock_open, read_data="{}")
    def test_custom_values_successful_script(self, mock_file, mock_subprocess, mock_os):
        """Test the function with custom script name and log file with successful execution"""
        script_name = "custom_backup.sh"
        log_file = "/home/user/custom_backup_log.json"
        result = task_func(script_name, log_file)
        self.assertIn('start_time', result)
        self.assertIn('end_time', result)
        self.assertEqual(result['exit_status'], 0)
    @patch("os.path.isfile", return_value=True)
    @patch("subprocess.call", return_value=0)
    @patch("builtins.open", new_callable=mock_open, read_data="{}")
    def test_log_data_format(self, mock_file, mock_subprocess, mock_os):
        """Test that the timestamps are in the correct format"""
        result = task_func()
        self.assertTrue(result['start_time'].count(":") == 2)
        self.assertTrue(result['end_time'].count(":") == 2)
    @patch("os.path.isfile", return_value=True)
    @patch("subprocess.call", return_value=1)
    @patch("builtins.open", new_callable=mock_open, read_data="{}")
    def test_non_zero_exit_status(self, mock_file, mock_subprocess, mock_os):
        """Test the function with a non-zero exit status"""
        result = task_func()
        self.assertEqual(result['exit_status'], 1)
