import seaborn as sns
import matplotlib.pyplot as plt
def task_func(df, target_values=[1, 3, 4]):
    df = df.applymap(lambda x: x if x in target_values else 0)
    plt.figure(figsize=(10, 5))
    for column in df.columns:
        sns.kdeplot(df[column], label=column, warn_singular=False)
    plt.legend()
    return df, plt.gca()
