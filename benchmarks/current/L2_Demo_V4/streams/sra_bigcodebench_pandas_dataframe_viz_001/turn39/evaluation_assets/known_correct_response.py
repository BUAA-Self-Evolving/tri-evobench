import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
def task_func(num_labels=5, data_range=(0, 1)):
    np.random.seed(0)
    columns = [f'Label{i + 1}' for i in range(num_labels)]
    data = pd.DataFrame(np.random.uniform(data_range[0], data_range[1], size=(num_labels, num_labels)), columns=columns)

    fig, ax = plt.subplots()

    data.plot(kind='bar', stacked=True, ax=ax)

    return fig