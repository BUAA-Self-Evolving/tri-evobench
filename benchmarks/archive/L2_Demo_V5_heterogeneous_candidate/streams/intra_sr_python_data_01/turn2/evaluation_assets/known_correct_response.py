import random
import numpy as np
def task_func(LETTERS):
    random_dict = {k: [random.randint(0, 100) for _ in range(random.randint(1, 10))] for k in LETTERS}
    mean_dict = {k: np.mean(v) for k, v in random_dict.items()}
    return mean_dict
