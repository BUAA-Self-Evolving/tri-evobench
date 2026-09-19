import pandas as pd
from sklearn.cluster import KMeans
import matplotlib.pyplot as plt
def task_func(df, n_clusters=3, random_state=0):
    if df.empty or not all(col in df.columns for col in ['group', 'date', 'value']):
        raise ValueError("DataFrame must be non-empty and contain 'group', 'date', and 'value' columns.")

    if not pd.api.types.is_datetime64_any_dtype(df['date']):
        raise ValueError("'date' column must be in datetime format.")

    df['date'] = df['date'].apply(lambda x: x.toordinal())
    X = df[['date', 'value']]

    kmeans = KMeans(n_clusters=n_clusters, random_state=random_state)
    kmeans.fit(X)
    y_kmeans = kmeans.predict(X)

    fig, ax = plt.subplots()
    ax.scatter(X['date'], X['value'], c=y_kmeans, cmap='viridis')
    ax.set_title('KMeans Clustering of Value vs Date')
    ax.set_xlabel('Date (ordinal)')
    ax.set_ylabel('Value')

    return ax