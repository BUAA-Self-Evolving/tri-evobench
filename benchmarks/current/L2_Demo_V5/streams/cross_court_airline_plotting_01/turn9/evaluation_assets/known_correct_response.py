from scipy.stats import linregress
import matplotlib.pyplot as plt
def task_func(data, column1, column2):
    if column1 not in data.columns or column2 not in data.columns:
        raise ValueError("Specified columns must exist in the DataFrame")

    x = data[column1].values
    y = data[column2].values

    slope, intercept, r_value, p_value, std_err = linregress(x, y)

    fig, ax = plt.subplots()
    ax.plot(x, y, 'o', label='original data')
    ax.plot(x, intercept + slope*x, 'r', label='fitted line')
    ax.legend()

    return (slope, intercept, r_value, p_value, std_err), ax
