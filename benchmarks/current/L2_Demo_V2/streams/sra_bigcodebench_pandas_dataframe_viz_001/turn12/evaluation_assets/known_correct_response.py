import pandas as pd
import seaborn as sns
def task_func(data):
    df = pd.DataFrame(data)
    correlation_matrix = df.corr()
    ax = sns.heatmap(correlation_matrix, annot=True, cmap='coolwarm')
    ax.set_title('Correlation Matrix')
    return ax