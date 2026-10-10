from sklearn.preprocessing import StandardScaler
import seaborn as sns
import matplotlib.pyplot as plt
def task_func(df):
    df = df.fillna(df.mean(axis=0))
    scaler = StandardScaler()
    df[df.columns] = scaler.fit_transform(df[df.columns])
    plt.figure(figsize=(10, 5))
    heatmap = sns.heatmap(df.corr(), annot=True, cmap="coolwarm")
    return df, heatmap
