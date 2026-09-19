import pandas as pd
import regex as re
from sklearn.feature_extraction.text import CountVectorizer
def task_func(text):
    sentences = re.split(r"\.\s*", text)
    sentences = [sentence for sentence in sentences if len(sentence.strip()) != 0]
    vectorizer = CountVectorizer()
    dtm = vectorizer.fit_transform(sentences)
    df = pd.DataFrame(dtm.toarray(), columns=vectorizer.get_feature_names_out())
    return df
