from sklearn.cluster import KMeans
import matplotlib.pyplot as plt
def task_func(df, age: int, height: int):
    # Filter the DataFrame based on given conditions
    selected_df = df[(df["Age"] > age) & (df["Height"] < height)].copy()

    # Apply KMeans clustering only if there are at least 3 rows in the filtered data
    if len(selected_df) >= 3:
        kmeans = KMeans(n_clusters=3)
        selected_df["Cluster"] = kmeans.fit_predict(selected_df[["Age", "Height"]])

        # Visualize the clusters
        plt.figure(figsize=(10, 5))
        plt.scatter(selected_df["Age"], selected_df["Height"], c=selected_df["Cluster"])
        plt.xlabel("Age")
        plt.ylabel("Height")
        plt.title("KMeans Clustering based on Age and Height")
        ax = plt.gca()
        return selected_df, ax
    else:
        selected_df["Cluster"] = 0
        return selected_df, None
