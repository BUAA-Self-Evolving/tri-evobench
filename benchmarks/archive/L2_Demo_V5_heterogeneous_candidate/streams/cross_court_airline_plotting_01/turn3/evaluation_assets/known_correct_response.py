import random
import matplotlib.pyplot as plt
import seaborn as sns
def task_func(result, colors=['b', 'g', 'r', 'c', 'm', 'y', 'k']):
    from_user_values = [d['from_user'] for d in result if 'from_user' in d]
    color = random.choice(colors)
    plt.figure()
    sns.histplot(from_user_values, color=color)
    plt.show()
