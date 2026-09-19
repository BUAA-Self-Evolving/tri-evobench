import random
import matplotlib.pyplot as plt
# Constants
SALARY_RANGE = (20000, 100000)
def task_func(dict1):
    emp_salaries = []

    for prefix, num_employees in dict1.items():
        if not prefix.startswith('EMPXX'):
            continue

        for _ in range(num_employees):
            salary = random.randint(*SALARY_RANGE)
            emp_salaries.append(salary)

    plt.hist(emp_salaries, bins=10, alpha=0.5)
    plt.title('Salary Distribution in EMPXX Department')
    plt.xlabel('Salary')
    plt.ylabel('Number of Employees')
    return plt.gca()
