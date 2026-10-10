import unittest
class TestCases(unittest.TestCase):
    def test_case_1(self):
        random.seed(42)
        d = {'EMPXX': 10, 'MANXX': 5, 'DEVXX': 8, 'HRXX': 7}
        ax = task_func(d)
        self.assertEqual(ax.get_title(), 'Salary Distribution in EMPXX Department')
        self.assertEqual(ax.get_xlabel(), 'Salary')
        self.assertEqual(ax.get_ylabel(), 'Number of Employees')
    def test_case_2(self):
        random.seed(42)
        d = {'EMPXX': 5, 'MANXX': 2, 'DEVXX': 3, 'HRXX': 4}
        ax = task_func(d)
        self.assertEqual(ax.get_title(), 'Salary Distribution in EMPXX Department')
        self.assertEqual(ax.get_xlabel(), 'Salary')
        self.assertEqual(ax.get_ylabel(), 'Number of Employees')
    def test_case_3(self):
        random.seed(42)
        d = {'EMPXX': 3, 'MANXX': 1, 'DEVXX': 1, 'HRXX': 7}
        ax = task_func(d)
        self.assertEqual(ax.get_title(), 'Salary Distribution in EMPXX Department')
        self.assertEqual(ax.get_xlabel(), 'Salary')
        self.assertEqual(ax.get_ylabel(), 'Number of Employees')
    def test_case_4(self):
        random.seed(42)
        d = {'EMPXX': 6, 'MANXX': 7, 'DEVXX': 2, 'HRXX': 1}
        ax = task_func(d)
        self.assertEqual(ax.get_title(), 'Salary Distribution in EMPXX Department')
        self.assertEqual(ax.get_xlabel(), 'Salary')
        self.assertEqual(ax.get_ylabel(), 'Number of Employees')
    def test_case_5(self):
        random.seed(42)
        d = {'EMPXX': 1, 'MANXX': 1, 'DEVXX': 1, 'HRXX': 1}
        ax = task_func(d)
        self.assertEqual(ax.get_title(), 'Salary Distribution in EMPXX Department')
        self.assertEqual(ax.get_xlabel(), 'Salary')
        self.assertEqual(ax.get_ylabel(), 'Number of Employees')
