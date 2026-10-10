import numpy as np
from scipy import stats
import matplotlib.pyplot as plt
def task_func(mu, sigma, num_samples):
    samples = np.random.normal(mu, sigma, num_samples)
    fig, ax = plt.subplots()
    ax.hist(samples, bins=30, density=True, alpha=0.6, color='g')

    xmin, xmax = plt.xlim()
    x = np.linspace(xmin, xmax, 100)
    p = stats.norm.pdf(x, mu, sigma)
    ax.plot(x, p, 'k', linewidth=2)

    ax.set_title('Normal Distribution')
    plt.show()
    return fig
