import unittest
import doctest
class TestCases(unittest.TestCase):
    def setUp(self):
        # Creating sample CSV files for testing
        self.csv_file_1 = "sample_1.csv"
        with open(self.csv_file_1, 'w', newline='') as csvfile:
            fieldnames = ['id', 'name', 'age']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow({'id': '1', 'name': 'John', 'age': '25'})
            writer.writerow({'id': '2', 'name': 'Doe', 'age': '30'})

        self.csv_file_2 = "sample_2.csv"
        with open(self.csv_file_2, 'w', newline='') as csvfile:
            fieldnames = ['product', 'price']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow({'product': 'apple', 'price': '0.5'})
            writer.writerow({'product': 'banana', 'price': '0.3'})
    def tearDown(self):
        # Cleaning up the created files after testing
        os.remove(self.csv_file_1)
        if os.path.exists(self.csv_file_1.split('.')[0] + '.json'):
            os.remove(self.csv_file_1.split('.')[0] + '.json')

        os.remove(self.csv_file_2)
        if os.path.exists(self.csv_file_2.split('.')[0] + '.json'):
            os.remove(self.csv_file_2.split('.')[0] + '.json')
    def test_case_1(self):
        # Testing with the first sample CSV
        json_file = task_func(self.csv_file_1)
        self.assertTrue(os.path.exists(json_file))
        with open(json_file, 'r') as f:
            data = json.load(f)
            self.assertEqual(len(data), 2)
            self.assertEqual(data[0]['id'], '1')
            self.assertEqual(data[0]['name'], 'John')
            self.assertEqual(data[0]['age'], '25')
    def test_case_2(self):
        # Testing with the second sample CSV
        json_file = task_func(self.csv_file_2)
        self.assertTrue(os.path.exists(json_file))
        with open(json_file, 'r') as f:
            data = json.load(f)
            self.assertEqual(len(data), 2)
            self.assertEqual(data[0]['product'], 'apple')
            self.assertEqual(data[0]['price'], '0.5')
    def test_case_3(self):
        # Testing with a non-existing file
        with self.assertRaises(FileNotFoundError):
            task_func("non_existing.csv")
    def test_case_4(self):
        # Testing with an empty CSV file
        empty_csv = "empty.csv"
        with open(empty_csv, 'w', newline='') as csvfile:
            pass
        json_file = task_func(empty_csv)
        self.assertTrue(os.path.exists(json_file))
        with open(json_file, 'r') as f:
            data = json.load(f)
            self.assertEqual(len(data), 0)
        os.remove(empty_csv)
        os.remove(empty_csv.split('.')[0] + '.json')
    def test_case_5(self):
        # Testing with a CSV file having only headers
        headers_csv = "headers_only.csv"
        with open(headers_csv, 'w', newline='') as csvfile:
            fieldnames = ['field1', 'field2']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
        json_file = task_func(headers_csv)
        self.assertTrue(os.path.exists(json_file))
        with open(json_file, 'r') as f:
            data = json.load(f)
            self.assertEqual(len(data), 0)
        os.remove(headers_csv)
        os.remove(headers_csv.split('.')[0] + '.json')
