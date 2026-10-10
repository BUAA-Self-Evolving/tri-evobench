import numpy as np
from functools import reduce
def task_func(list_of_pairs):
    second_values = [pair[1] for pair in list_of_pairs]
    product = reduce(np.multiply, second_values)
    product_array = np.array([product])

    return product_array
