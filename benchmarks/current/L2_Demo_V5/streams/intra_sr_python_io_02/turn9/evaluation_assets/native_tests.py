import unittest
class TestCases(unittest.TestCase):
    """Test cases for the task_func function."""
    def test_case_1(self):
        text = "This is a sample sentence. This sentence contains sample words."
        expected_output = pd.Series({"Sentence 1": 5, "Sentence 2": 4})
        result = task_func(text)
        pd.testing.assert_series_equal(result, expected_output)
    def test_case_2(self):
        text = "Hello. My name is Marc. I'm here to help. How can I assist you today?"
        expected_output = pd.Series(
            {"Sentence 1": 1, "Sentence 2": 4, "Sentence 3": 3, "Sentence 4": 6}
        )
        result = task_func(text)
        pd.testing.assert_series_equal(result, expected_output)
    def test_case_3(self):
        text = "This is a test. Stopwords are words which do not contain important meaning."
        expected_output = pd.Series({"Sentence 1": 4, "Sentence 2": 7})
        result = task_func(text)
        pd.testing.assert_series_equal(result, expected_output)
    def test_case_4(self):
        text = "Hello! How are you? I'm fine, thanks."
        expected_output = pd.Series(
            {"Sentence 1": 6}
        )  # Only the last sentence is split by a period
        result = task_func(text)
        pd.testing.assert_series_equal(result, expected_output)
    def test_case_5(self):
        text = ""
        expected_output = pd.Series()
        result = task_func(text)
        pd.testing.assert_series_equal(result, expected_output)
