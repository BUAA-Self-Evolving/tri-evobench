import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
def task_func(csv_file_path: str, title: str):
    data = pd.read_csv(csv_file_path)
    corr = data.corr().round(2)
    plt.figure(figsize=(10, 8))
    sns.heatmap(corr, annot=True, cmap='coolwarm', cbar=True)
    plt.title(title)
    return corr, plt.gca()