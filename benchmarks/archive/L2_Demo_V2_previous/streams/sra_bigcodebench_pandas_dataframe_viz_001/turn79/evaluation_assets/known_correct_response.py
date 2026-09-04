import pandas as pd
import seaborn as sns
def task_func(data):
    df = pd.DataFrame(data)
    numeric_df = df.select_dtypes(include=["number"])
    if numeric_df.empty:
        raise ValueError("No numeric columns present")

    df_cumsum = numeric_df.cumsum()
    ax = sns.heatmap(df_cumsum)
    return ax