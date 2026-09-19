import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
def task_func(list_of_pairs):
    df = pd.DataFrame(list_of_pairs, columns=["Category", "Value"])
    plt.figure(figsize=(10, 5))
    sns.barplot(x="Category", y="Value", data=df)
    plt.title("Category vs Value")
    ax = plt.gca()
    return df, ax