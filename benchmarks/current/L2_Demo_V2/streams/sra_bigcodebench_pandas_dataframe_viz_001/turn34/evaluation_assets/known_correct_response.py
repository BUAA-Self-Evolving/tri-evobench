import pandas as pd
import matplotlib.pyplot as plt
def task_func(matrix):
    df = pd.DataFrame(matrix)

    fig, ax = plt.subplots()
    ax.imshow(df, cmap='hot', interpolation='nearest')

    return ax