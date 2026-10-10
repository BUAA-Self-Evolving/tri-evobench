import pandas as pd
import regex as re
# Constants
STOPWORDS = ["a", "an", "the", "in", "is", "are"]
def task_func(text):
    words = re.findall(r"\b\w+\b", text.lower())
    words = [word for word in words if word not in STOPWORDS]
    word_counts = pd.Series(words).value_counts().rename(None)
    return word_counts
