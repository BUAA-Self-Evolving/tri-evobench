import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt
# Constants
COLUMNS = ['col1', 'col2', 'col3']
def task_func(data):
    df = pd.DataFrame(data, columns=COLUMNS)
    analyzed_df = df.groupby(COLUMNS[:-1])[COLUMNS[-1]].nunique().reset_index()
    analyzed_df = analyzed_df.pivot(index=COLUMNS[0], columns=COLUMNS[1], values=COLUMNS[2])
    ax = sns.heatmap(analyzed_df, annot=True)
    plt.show()
    return analyzed_df, ax