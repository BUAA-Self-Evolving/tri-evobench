import pandas as pd

test = pd.read_csv("episodes/E003/data/test.csv")
pd.DataFrame({"PassengerId": test["PassengerId"], "Survived": 0}).to_csv("submission.csv", index=False)
