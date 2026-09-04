import pandas as pd
import matplotlib.pyplot as plt
def task_func(csv_file_path, col1_name="column1", col2_name="column2"):
    df = pd.read_csv(csv_file_path)
    groupby_data = df.groupby(col1_name)[col2_name].mean()

    _, ax = plt.subplots(figsize=(10, 6))
    ax.bar(groupby_data.index, groupby_data.values)
    ax.set_title(f"Mean of {col2_name} Grouped by {col1_name}")
    ax.set_xlabel(col1_name)
    ax.set_ylabel(f"Mean of {col2_name}")

    return ax