import pandas as pd
import matplotlib.pyplot as plt
from sklearn.preprocessing import MinMaxScaler
# Constants
PLOT_TITLE = "Scaled Values"
def task_func(data_dict):
    df = pd.DataFrame(data_dict).dropna()

    if df.empty:
        ax = plt.gca()
        ax.set_title(PLOT_TITLE)
        return df, ax

    scaler = MinMaxScaler()
    scaled_data = scaler.fit_transform(df)
    df_scaled = pd.DataFrame(scaled_data, columns=df.columns)

    ax = df_scaled.plot()
    ax.set_title(PLOT_TITLE)

    return df_scaled, ax