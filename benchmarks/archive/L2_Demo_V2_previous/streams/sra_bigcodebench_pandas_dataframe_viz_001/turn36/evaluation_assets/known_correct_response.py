import pandas as pd
from sklearn.preprocessing import StandardScaler
import matplotlib.pyplot as plt
# Constants
FEATURE_NAMES = ["Feature 1", "Feature 2", "Feature 3", "Feature 4", "Feature 5"]
def task_func(data_matrix):
    scaler = StandardScaler()
    standardized_data = scaler.fit_transform(data_matrix)
    df = pd.DataFrame(standardized_data, columns=FEATURE_NAMES)
    df["Mean"] = df.mean(axis=1)
    plt.figure(figsize=(10, 5))
    ax = df["Mean"].plot(kind="hist", title="Distribution of Means")
    return df, ax