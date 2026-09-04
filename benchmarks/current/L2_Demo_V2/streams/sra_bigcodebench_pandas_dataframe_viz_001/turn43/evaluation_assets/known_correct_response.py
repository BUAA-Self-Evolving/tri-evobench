import matplotlib.pyplot as plt
import seaborn as sns
import pandas as pd
from sklearn.datasets import load_diabetes
def task_func():
    font = {'family': 'Arial'}
    plt.rc('font', **font)  # Set the global font to Arial.
    DIABETES = load_diabetes()
    diabetes_df = pd.DataFrame(data=DIABETES.data, columns=DIABETES.feature_names)
    pair_plot = sns.pairplot(diabetes_df)
    return pair_plot.fig, diabetes_df