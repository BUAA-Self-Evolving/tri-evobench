import pandas as pd

test = pd.read_csv("data/test.csv")
submission = pd.DataFrame({"PassengerId": test["PassengerId"], "Survived": (test["Sex"] == "female").astype(int)})
submission.to_csv("submission.csv", index=False)
