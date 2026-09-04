import pandas as pd
import matplotlib.pyplot as plt
def task_func(data_list):
    df = pd.DataFrame(data_list)
    fig, ax = plt.subplots()
    for column in df:
        ax.plot(df[column], label=column)
    ax.set_title("Student Scores over Tests")
    ax.set_xlabel("Test Number")
    ax.set_ylabel("Score")

    return ax