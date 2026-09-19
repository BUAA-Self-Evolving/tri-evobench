import numpy as np
from scipy import stats
import matplotlib.pyplot as plt
TARGET_VALUES = np.array([1, 3, 4])
def task_func(df):
    # Ensure the DataFrame contains only positive values
    if (df <= 0).any().any():
        raise ValueError("Input DataFrame should contain only positive values.")

    df = df.applymap(lambda x: x if x in TARGET_VALUES else 0)

    transformed_df = pd.DataFrame()

    fig, ax = plt.subplots()

    for column in df.columns:
        # Check if data is constant
        if df[column].nunique() == 1:
            transformed_df[column] = df[column]
        else:
            transformed_data, _ = stats.boxcox(
                df[column] + 1
            )  # Add 1 since the are some null values
            transformed_df[column] = transformed_data

            # Using matplotlib's kde method to plot the KDE
            kde = stats.gaussian_kde(transformed_df[column])
            x_vals = np.linspace(
                min(transformed_df[column]), max(transformed_df[column]), 1000
            )
            ax.plot(x_vals, kde(x_vals), label=column)

    ax.legend()
    plt.show()
    return transformed_df, fig
