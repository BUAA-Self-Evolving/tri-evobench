import pandas as pd
import matplotlib.pyplot as plt
from random import randint
def task_func(num_types=5, integer_range=(0, 100)):
    LABELS = [f'Type{i + 1}' for i in range(num_types)]
    data = pd.DataFrame({label: [randint(*integer_range) for _ in range(num_types)] for label in LABELS})

    fig, ax = plt.subplots()
    data.plot(kind='barh', stacked=True, ax=ax)

    return fig, ax