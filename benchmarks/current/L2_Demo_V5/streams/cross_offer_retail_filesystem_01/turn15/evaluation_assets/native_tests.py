import unittest
import tempfile
import doctest
class TestCases(unittest.TestCase):
    def setUp(self):
        self.json_file = tempfile.NamedTemporaryFile(delete=False)
    def tearDown(self):
        os.unlink(self.json_file.name)
    def test_case_1(self):
        data = {'key1': 'value1', 'key2': 'value2', 'key3': 'value1'}
        result_path = task_func(data, self.json_file.name)
        self.assertTrue(os.path.exists(result_path), "JSON file doesn't exist.")
        with open(result_path, 'r') as f:
            json_data = json.load(f)
            self.assertEqual(json_data['data']['a'], 1)
            self.assertEqual(json_data['freq']['value1'], 2)

    def test_case_2(self):
        data = {}
        result_path = task_func(data, self.json_file.name)
        self.assertTrue(os.path.exists(result_path), "JSON file doesn't exist.")
        with open(result_path, 'r') as f:
            json_data = json.load(f)
            self.assertEqual(json_data['data']['a'], 1)
            self.assertEqual(json_data['freq']['1'], 1)

    def test_case_3(self):
        data = {'x': 'y', 'z': 'y'}
        result_path = task_func(data, self.json_file.name)
        self.assertTrue(os.path.exists(result_path), "JSON file doesn't exist.")
        with open(result_path, 'r') as f:
            json_data = json.load(f)
            self.assertEqual(json_data['data']['a'], 1)
            self.assertEqual(json_data['freq']['y'], 2)

    def test_case_4(self):
        data = {'e': 'b', 'c': 'd'}
        result_path = task_func(data, self.json_file.name)
        self.assertTrue(os.path.exists(result_path), "JSON file doesn't exist.")
        with open(result_path, 'r') as f:
            json_data = json.load(f)
            self.assertEqual(json_data['data']['a'], 1)
            self.assertEqual(json_data['freq']['b'], 1)

    def test_case_5(self):
        data = {'apple': 'fruit', 'carrot': 'vegetable'}
        result_path = task_func(data, self.json_file.name)
        self.assertTrue(os.path.exists(result_path), "JSON file doesn't exist.")
        with open(result_path, 'r') as f:
            json_data = json.load(f)
            self.assertEqual(json_data['data']['a'], 1)
            self.assertEqual(json_data['freq']['fruit'], 1)
