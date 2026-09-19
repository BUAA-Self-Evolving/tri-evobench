import unittest
import os
class TestCases(unittest.TestCase):
    """Test cases for the task_func function."""
    def setUp(self):
        self.test_dir = "data/task_func"
        os.makedirs(self.test_dir, exist_ok=True)
        self.o_1 = os.path.join(self.test_dir, "histogram_1.png")
    def tearDown(self) -> None:
        import shutil
        try:
            shutil.rmtree(self.test_dir)
        except:
            pass
    def test_case_1(self):
        random.seed(42)
        result = task_func(10)
        self.assertEqual(len(result), 10)
    def test_case_2(self):
        random.seed(42)
        result = task_func(15)
        for timestamp in result:
            try:
                datetime.strptime(timestamp, DATE_FORMAT)
            except ValueError:
                self.fail(f"Timestamp {timestamp} doesn't match the specified format.")
    def test_case_3(self):
        random.seed(42)
        task_func(20, output_path=self.o_1)
        self.assertTrue(os.path.exists(self.o_1))
    def test_case_4(self):
        result = task_func(50)
        self.assertEqual(len(result), len(set(result)))
    def test_case_5(self):
        result = task_func(0)
        self.assertEqual(len(result), 0)
