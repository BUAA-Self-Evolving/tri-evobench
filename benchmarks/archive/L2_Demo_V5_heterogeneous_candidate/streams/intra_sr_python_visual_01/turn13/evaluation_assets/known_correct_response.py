from sklearn.preprocessing import MinMaxScaler
import matplotlib.pyplot as plt
def task_func(df):
    df = df.fillna(df.mean(axis=0))
    scaler = MinMaxScaler()
    df[df.columns] = scaler.fit_transform(df[df.columns])
    plt.figure(figsize=(10, 5))
    df.boxplot(grid=False, vert=False, fontsize=15)
    return df, plt.gca()
