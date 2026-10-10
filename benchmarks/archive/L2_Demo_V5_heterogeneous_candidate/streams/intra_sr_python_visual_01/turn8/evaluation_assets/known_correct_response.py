from datetime import datetime
import pandas as pd
import matplotlib.pyplot as plt
# Constants
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
def task_func(timestamps):
    if not timestamps:
        raise ValueError("Input list of timestamps is empty.")
    datetimes = [datetime.fromtimestamp(t).strftime(DATE_FORMAT) for t in timestamps]
    df = pd.DataFrame({"Timestamp": timestamps, "Datetime": datetimes})
    ax = plt.hist(pd.to_datetime(df["Datetime"]))
    plt.close()
    return df, ax
