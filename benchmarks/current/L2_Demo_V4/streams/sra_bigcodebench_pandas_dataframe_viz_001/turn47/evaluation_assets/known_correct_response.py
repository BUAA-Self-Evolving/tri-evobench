import numpy as np
import pandas as pd
import seaborn as sns
def task_func(num_samples=100, num_features=5):
    FEATURES = ['Feature' + str(i) for i in range(1, num_features + 1)]
    SAMPLES = ['Sample' + str(i) for i in range(1, num_samples + 1)]
    
    data = np.random.rand(len(SAMPLES), len(FEATURES))
    df = pd.DataFrame(data, index=SAMPLES, columns=FEATURES)
    
    corr_matrix = df.corr()
    ax = sns.heatmap(corr_matrix, annot=True)
    
    return df, ax