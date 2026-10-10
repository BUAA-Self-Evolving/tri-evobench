import numpy as np
from itertools import zip_longest
def task_func(l1, l2,THRESHOLD = 0.5):
    combined = [val for pair in zip_longest(l1, l2) for val in pair if val is not None]
    differences = np.abs(np.array(combined) - THRESHOLD)
    closest_index = np.argmin(differences)
    return combined[closest_index]
