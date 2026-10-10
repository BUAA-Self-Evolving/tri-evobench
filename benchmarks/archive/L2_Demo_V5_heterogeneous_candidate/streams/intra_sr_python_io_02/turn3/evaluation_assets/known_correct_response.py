import pandas as pd
import regex as re
import seaborn as sns
import matplotlib.pyplot as plt
COLUMN_NAMES = ["Name", "Email", "Age", "Country"]
def task_func(text):
    pattern = r"Name: (.*?), Email: (.*?), Age: (.*?), Country: (.*?)($|\n)"
    matches = re.findall(pattern, text)
    data = []
    for match in matches:
        data.append(match[:-1])
    df = pd.DataFrame(data, columns=COLUMN_NAMES)
    df["Age"] = df["Age"].astype(int)
    sns.histplot(data=df, x="Age")
    plt.show()
    return df
