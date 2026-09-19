import numpy as np
from scipy.stats import ttest_1samp
import matplotlib.pyplot as plt
# Constants
ALPHA = 0.05
def task_func(data_matrix):
    means = np.mean(data_matrix, axis=1)
    population_mean = np.mean(data_matrix)

    _, p_value = ttest_1samp(means, population_mean)
    significant_indices = np.where(p_value < ALPHA)[0]

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(means, "ro", label="Means")
    ax.plot(
        significant_indices, means[significant_indices], "bo", label="Significant Means"
    )
    ax.axhline(y=population_mean, color="g", linestyle="-", label="Population Mean")
    ax.legend()
    return significant_indices.tolist(), ax
