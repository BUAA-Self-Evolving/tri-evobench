import time
from datetime import datetime
import random
import matplotlib.pyplot as plt
# Constants
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
def task_func(n, output_path=None):
    timestamps = []
    for _ in range(n):
        timestamp = random.randint(0, int(time.time()))
        formatted_time = datetime.utcfromtimestamp(timestamp).strftime(DATE_FORMAT)
        timestamps.append(formatted_time)

    plt.hist([datetime.strptime(t, DATE_FORMAT) for t in timestamps])

    if output_path:
        plt.savefig(output_path)
    else:
        plt.show()
    return timestamps
