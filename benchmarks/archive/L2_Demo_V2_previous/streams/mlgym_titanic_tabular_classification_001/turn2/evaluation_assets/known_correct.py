import pandas as pd

test = pd.read_csv("episodes/E002/data/test.csv")
submission = pd.DataFrame({"PassengerId": test["PassengerId"], "Survived": (test["Sex"] == "female").astype(int)})
submission.to_csv("submission.csv", index=False)
