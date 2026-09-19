import unittest
import doctest
class TestCases(unittest.TestCase):
    
    def test_case_1(self):
        d, data, _ = task_func({'key1': 10, 'key2': 20}, 'newkey', '25', n=500)
        self.assertIn('newkey', d)
        self.assertEqual(int(d['newkey']), 25)
        self.assertEqual(len(data), 500)
        
    def test_case_2(self):
        d, data, _ = task_func({}, 'firstkey', '15', n=300)
        self.assertIn('firstkey', d)
        self.assertEqual(int(d['firstkey']), 15)
        self.assertEqual(len(data), 300)
        
    def test_case_3(self):
        d, data, ax = task_func({'a': 5}, 'b', '10', n=1000)
        self.assertIn('b', d)
        self.assertEqual(int(d['b']), 10)
        self.assertEqual(len(data), 1000)
        # Test the histogram plot
        self.assertEqual(len(ax.patches), 30)
        # Test the axes data
        self.assertAlmostEqual(ax.get_xlim()[1], 40.5, places=1)
        self.assertAlmostEqual(ax.get_ylim()[1], 0.05, places=1)
        
    def test_case_4(self):
        d, data, _ = task_func({'x': 50}, 'y', '75', n=10, seed=77)
        self.assertIn('y', d)
        self.assertEqual(int(d['y']), 75)
        self.assertEqual(len(data), 10)
        # Test the generated data
        self.assertTrue(np.allclose(data, np.array(
            [ 91.83, 124.61, 31.51, 105.58, 109.98, -73.1,  95.66, -43.18, 192.62,  20.64]
        ), atol=0.01))
        
    def test_case_5(self):
        d, data, _ = task_func({'1': 100}, '2', '200', n=700)
        self.assertIn('2', d)
        self.assertEqual(int(d['2']), 200)
        self.assertEqual(len(data), 700)