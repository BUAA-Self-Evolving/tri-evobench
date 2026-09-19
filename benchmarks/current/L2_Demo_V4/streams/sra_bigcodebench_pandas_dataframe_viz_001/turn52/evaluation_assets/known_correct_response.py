import pandas as pd
import seaborn as sns
def task_func(data='/path/to/data.csv', emp_prefix='EMP'):
    # Load data and filter
    df = pd.read_csv(data)
    df = df[df['Employee ID'].str.startswith(emp_prefix)]

    # Plot histogram
    ax = sns.histplot(data=df, x='Age', kde=True)

    return df, ax