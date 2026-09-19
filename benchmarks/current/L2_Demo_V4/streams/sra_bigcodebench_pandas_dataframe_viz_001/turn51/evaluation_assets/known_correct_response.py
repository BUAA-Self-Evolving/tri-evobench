import pandas as pd
from matplotlib import pyplot as plt
def task_func(arr):
    if not arr.size:  # Check for empty array
        _, ax = plt.subplots()
        ax.set_title("Time Series of Row Sums")
        return ax

    row_sums = arr.sum(axis=1)
    df = pd.DataFrame(row_sums, columns=["Sum"])
    df.index = pd.date_range(start="1/1/2020", periods=df.shape[0])
    ax = df.plot(title="Time Series of Row Sums")
    return ax