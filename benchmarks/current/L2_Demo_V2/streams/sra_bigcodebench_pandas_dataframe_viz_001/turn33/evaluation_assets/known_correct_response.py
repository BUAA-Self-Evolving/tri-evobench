import pandas as pd
import seaborn as sns
# Constants
COLUMNS = ['col1', 'col2', 'col3']
def task_func(data):
    df = pd.DataFrame(data, columns=COLUMNS)
    analyzed_df = df.groupby(COLUMNS[:-1])[COLUMNS[-1]].nunique().reset_index()
    ax = sns.distplot(analyzed_df[COLUMNS[-1]])

    return analyzed_df, ax