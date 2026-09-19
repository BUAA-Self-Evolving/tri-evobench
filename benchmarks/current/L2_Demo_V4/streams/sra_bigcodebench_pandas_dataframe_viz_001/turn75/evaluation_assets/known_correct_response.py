import pandas as pd
import seaborn as sns
import numpy as np
# Constants
LABELS = ['H\u2082O', 'O\u2082', 'CO\u2082', 'N\u2082', 'Ar']
def task_func(x, y, labels):
    data = []

    for i in range(len(x)):
        data.append(np.concatenate((x[i], y[i])))

    df = pd.DataFrame(data, index=labels)
    ax = sns.heatmap(df, cmap='coolwarm')
    
    return ax, df