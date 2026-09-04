import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
def task_func(num_groups=5, data_size=5, labels=None):

    # If labels are not provided, generate default labels
    if labels is None:
        labels = [f'Group{i + 1}' for i in range(num_groups)]

    # Generate random data
    data = pd.DataFrame(np.random.rand(data_size, num_groups), columns=labels)

    # Plot data
    fig, ax = plt.subplots()
    data.plot(kind='bar', stacked=True, ax=ax)

    # Save the plot for verification in tests
    plot_filename = 'test_plot.png'
    fig.savefig(plot_filename)

    return fig, data, plot_filename