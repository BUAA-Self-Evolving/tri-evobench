import pandas as pd
import matplotlib.pyplot as plt
from random import randint
def task_func(num_rows=5, rand_range=(0, 100)):
    labels = ['A', 'B', 'C', 'D', 'E']
    data = pd.DataFrame({label: [randint(rand_range[0], rand_range[1]) for _ in range(num_rows)] for label in labels})

    fig, ax = plt.subplots()

    data.plot(kind='bar', stacked=True, ax=ax)

    return fig