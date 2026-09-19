import unittest
import numpy as np
class TestCases(unittest.TestCase):
    def test_case_1(self):
        df = pd.DataFrame(np.random.randint(0, 100, size=(100, 5)), columns=list('ABCDE'))
        tuples = [(10, 20, 30, 40, 50), (60, 70, 80, 90, 100)]
        modified_df, plots = task_func(df, tuples, 3)
        # Convert tuples to DataFrame for compatibility
        tuples_df = pd.DataFrame([t for t in tuples], columns=list('ABCDE'))
        # Check each tuple to ensure it's not in modified_df
        for _, row in tuples_df.iterrows():
            # Use merge to find matching rows, which is empty if no match exists
            merged_df = pd.merge(modified_df, pd.DataFrame([row]), on=list('ABCDE'))
            self.assertTrue(merged_df.empty, f"Tuple {tuple(row)} found in modified DataFrame.")
    def test_case_2(self):
        df = pd.DataFrame(np.random.randint(0,100,size=(100, 5)), columns=list('ABCDE'))
        tuples = [(10, 20, 30, 40, 50), (60, 70, 80, 90, 100)]
        modified_df, plots = task_func(df, tuples, 2)
        
        for plot in plots:
            self.assertTrue(plot.x.name in df.columns)
            self.assertTrue(plot.y.name in df.columns)
    
    def test_case_3(self):
        df = pd.DataFrame(columns=list('ABCDE'))
        tuples = [(10, 20, 30, 40, 50)]
        modified_df, plots = task_func(df, tuples, 2)
        
        self.assertTrue(modified_df.empty)
        self.assertEqual(len(plots), 0)
    
    def test_case_4(self):
        df = pd.DataFrame([(10, 20, 30, 40, 50), (10, 20, 30, 40, 50)], columns=list('ABCDE'))
        tuples = [(10, 20, 30, 40, 50)]
        modified_df, plots = task_func(df, tuples, 2)
        
        self.assertTrue(modified_df.empty)
        self.assertEqual(len(plots), 0)
    
    def test_case_5(self):
        df = pd.DataFrame(np.random.randint(0,100,size=(100, 5)), columns=list('ABCDE'))
        tuples = []
        modified_df, plots = task_func(df, tuples, 2)
        
        pd.testing.assert_frame_equal(modified_df, df)
        self.assertEqual(len(plots), 2)