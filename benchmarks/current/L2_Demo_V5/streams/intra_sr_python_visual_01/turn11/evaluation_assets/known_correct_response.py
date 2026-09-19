from datetime import datetime
import pandas as pd
import pytz
import matplotlib.pyplot as plt
# Constants
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
TIMEZONES = [
    "America/New_York",
    "Europe/London",
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Australia/Sydney",
]
def task_func(timestamp):
    datetimes = [
        datetime.fromtimestamp(timestamp, pytz.timezone(tz)).strftime(DATE_FORMAT)
        for tz in TIMEZONES
    ]
    df = pd.DataFrame({"Timezone": TIMEZONES, "Datetime": datetimes})
    df["Datetime"] = pd.to_datetime(df["Datetime"])
    ax = df.plot.bar(x="Timezone", y="Datetime", legend=False)
    plt.ylabel("Timezone")
    plt.ylabel("Datetime")
    plt.title("Datetime = f(Timezone)")
    plt.close()
    return df, ax
