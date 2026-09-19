import numpy as np
import seaborn as sns
def task_func(df):
    df = df.fillna(df.mean(axis=0))
    description = df.describe()
    plots = []
    for col in df.select_dtypes(include=[np.number]).columns:
        plot = sns.displot(df[col], bins=10)
        plots.append(plot.ax)
    return description, plots
