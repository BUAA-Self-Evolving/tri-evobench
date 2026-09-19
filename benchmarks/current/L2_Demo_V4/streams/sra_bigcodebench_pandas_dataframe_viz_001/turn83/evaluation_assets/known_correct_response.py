import pandas as pd
import matplotlib.pyplot as plt
from random import sample
# Constants for column names to use in plots
COLUMNS = ['A', 'B', 'C', 'D', 'E']
def task_func(df: pd.DataFrame, tuples: list, n_plots: int) -> (pd.DataFrame, list):

    # Ensure tuple elements match DataFrame columns for removal
    df = df[~df.apply(tuple, axis=1).isin(tuples)]

    # Generate random plots
    plots = []
    for _ in range(n_plots):
        selected_columns = sample(COLUMNS, 2)
        ax = df.plot(x=selected_columns[0], y=selected_columns[1], kind='scatter')
        plots.append(ax)

    plt.show()

    return df, plots