from scipy.stats import zscore
import matplotlib.pyplot as plt
def task_func(df):
    # Fill missing values with column's average
    df = df.fillna(df.mean(axis=0))
    # Compute Z-scores
    df = df.apply(zscore)
    # Plot histograms for each numeric column
    axes = df.hist(grid=False, bins=10, layout=(1, df.shape[1]))
    plt.tight_layout()
    return df, axes
