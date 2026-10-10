import re
import pandas as pd
STOPWORDS = ["Those", "are", "the", "words", "to", "ignore"]
def task_func(text):
    sentences = re.split(r"\.\s*", text)
    sentence_counts = {}

    for i, sentence in enumerate(sentences):
        if sentence.strip() == "":
            continue
        words = re.split(r"\s+", sentence.lower())
        words = [word for word in words if word not in STOPWORDS]
        sentence_counts[f"Sentence {i+1}"] = len(words)

    sentence_counts = pd.Series(sentence_counts)
    return sentence_counts
