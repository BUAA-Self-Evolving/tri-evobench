import pandas as pd

test = pd.read_csv("data/test.csv")
pd.DataFrame({"PassengerId": test["PassengerId"], "Survived": 0}).to_csv("submission.csv", index=False)
