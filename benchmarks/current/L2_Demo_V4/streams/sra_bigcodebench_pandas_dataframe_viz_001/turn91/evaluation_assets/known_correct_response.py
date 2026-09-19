import pandas as pd
import matplotlib.pyplot as plt
from sklearn.decomposition import PCA
def task_func(data_matrix, n_components=2):
    pca = PCA(n_components=n_components)
    transformed_data = pca.fit_transform(data_matrix)

    df = pd.DataFrame(
        transformed_data,
        columns=[f"Component {i+1}" for i in range(transformed_data.shape[1])],
    )
    df["Mean"] = df.mean(axis=1)

    fig, ax = plt.subplots()
    ax.plot(np.cumsum(pca.explained_variance_ratio_))
    ax.set_xlabel("Number of Components")
    ax.set_ylabel("Cumulative Explained Variance")
    return df, ax