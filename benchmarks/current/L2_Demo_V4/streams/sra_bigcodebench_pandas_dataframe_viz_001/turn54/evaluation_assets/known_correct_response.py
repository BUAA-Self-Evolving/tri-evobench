import pandas as pd
import matplotlib.pyplot as plt
# Constants
COLUMN_NAMES = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H']
def task_func(data):
    df = pd.DataFrame(data, columns=COLUMN_NAMES)
    df['Average'] = df.mean(axis=1)

    # Creating a new figure and axis for plotting
    fig, ax = plt.subplots()
    df['Average'].plot(ax=ax)
    ax.set_ylabel('Average')  # Setting the Y-axis label to 'Average'

    return df, ax