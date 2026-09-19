import unittest
class TestCases(unittest.TestCase):
    """Test cases for the task_func function."""
    def test_case_1(self):
        data = [[1, 1, 1], [1, 1, 1], [1, 1, 2], [1, 2, 3], [1, 2, 3], [1, 2, 3], [2, 1, 1], [2, 1, 2], [2, 1, 3], [2, 2, 3], [2, 2, 3], [2, 2, 3]]
        df = pd.DataFrame(data, columns=COLUMNS)
        analyzed_df, ax = task_func(df)
        expected_data = [[1, 1, 2], [1, 2, 1], [2, 1, 3], [2, 2, 1]]
        expected_df = pd.DataFrame(expected_data, columns=COLUMNS)
        expected_df = expected_df.pivot(index=COLUMNS[0], columns=COLUMNS[1], values=COLUMNS[2])
        # Assertions
        self.assertTrue(isinstance(analyzed_df, pd.DataFrame))
        pd.testing.assert_frame_equal(analyzed_df, expected_df, check_dtype=False)
        self.assertTrue(isinstance(ax, plt.Axes))
    def test_case_2(self):
        data = [
            [1, 1, 2],
            [1, 1, 3],
            [1, 2, 4],
            [1, 1, 5],
            [1, 3, 7]
        ]
        analyzed_df, ax = task_func(data)
        expected_data = [
            [1, 1, 3],
            [1, 2, 1],
            [1, 3, 1]
        ]
        expected_df = pd.DataFrame(expected_data, columns=COLUMNS)
        expected_df = expected_df.pivot(index=COLUMNS[0], columns=COLUMNS[1], values=COLUMNS[2])
        # Assertions
        self.assertTrue(isinstance(analyzed_df, pd.DataFrame))
        pd.testing.assert_frame_equal(analyzed_df, expected_df, check_dtype=False)
        self.assertTrue(isinstance(ax, plt.Axes))
    def test_case_3(self):
        data = [
            [1, 1, 1],
            [1, 2, 3],
            [2, 1, 4],
            [2, 2, 5]
        ]
        analyzed_df, ax = task_func(data)
        expected_data = [
            [1, 1, 1],
            [1, 2, 1],
            [2, 1, 1],
            [2, 2, 1]
        ]
        expected_df = pd.DataFrame(expected_data, columns=COLUMNS)
        expected_df = expected_df.pivot(index=COLUMNS[0], columns=COLUMNS[1], values=COLUMNS[2])
        # Assertions
        self.assertTrue(isinstance(analyzed_df, pd.DataFrame))
        pd.testing.assert_frame_equal(analyzed_df, expected_df, check_dtype=False)
        self.assertTrue(isinstance(ax, plt.Axes))
    def test_case_4(self):
        data = [
            [1, 1, 1],
            [1, 1, 1],
            [1, 1, 1]
        ]
        analyzed_df, ax = task_func(data)
        expected_data = [
            [1, 1, 1],
        ]
        expected_df = pd.DataFrame(expected_data, columns=COLUMNS)
        expected_df = expected_df.pivot(index=COLUMNS[0], columns=COLUMNS[1], values=COLUMNS[2])
        # Assertions
        self.assertTrue(isinstance(analyzed_df, pd.DataFrame))
        pd.testing.assert_frame_equal(analyzed_df, expected_df, check_dtype=False)
        self.assertTrue(isinstance(ax, plt.Axes))
    def test_case_5(self):
        data = [
            [0, 0, 0],
            [0, 1, 0],
            [1, 0, 0],
            [1, 1, 0],
            [0, 0, 1],
            [0, 1, 1],
            [1, 0, 1],
            [1, 1, 1],
        ]
        analyzed_df, ax = task_func(data)
        expected_data = [
            [0, 0, 2],
            [0, 1, 2],
            [1, 0, 2],
            [1, 1, 2]
        ]
        expected_df = pd.DataFrame(expected_data, columns=COLUMNS)
        expected_df = expected_df.pivot(index=COLUMNS[0], columns=COLUMNS[1], values=COLUMNS[2])
        # Assertions
        self.assertTrue(isinstance(analyzed_df, pd.DataFrame))
        pd.testing.assert_frame_equal(analyzed_df, expected_df, check_dtype=False)
        self.assertTrue(isinstance(ax, plt.Axes))