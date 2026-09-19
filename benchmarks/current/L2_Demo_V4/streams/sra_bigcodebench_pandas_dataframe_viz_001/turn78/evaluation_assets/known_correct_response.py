import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
PRODUCTS = ['Product' + str(i) for i in range(1, 6)]
MONTHS = ['Month' + str(i) for i in range(1, 13)]
def task_func():
    sales = np.random.randint(100, 1001, size=(len(MONTHS), len(PRODUCTS)))
    df = pd.DataFrame(sales, index=MONTHS, columns=PRODUCTS)

    # Visualizations
    total_sales = df.sum()
    plt.figure(figsize=(10, 5))
    total_sales.plot(kind='line', title='Total Sales per Product')
    plt.ylabel('Total Sales')
    plt.show()

    plt.figure(figsize=(10, 8))
    sns.heatmap(df, annot=True, fmt="d", cmap='viridis')
    plt.title('Monthly Sales per Product')
    plt.show()

    return df