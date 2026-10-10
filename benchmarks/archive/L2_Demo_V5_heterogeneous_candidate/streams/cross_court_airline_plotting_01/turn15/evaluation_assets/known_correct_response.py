import re
import matplotlib.pyplot as plt
def task_func(df):

    if df.empty or 'Likes' not in df.columns or 'Views' not in df.columns or 'Title' not in df.columns:
        fig, ax = plt.subplots()
        return ax

    pattern = re.compile(r'(how|what)', re.IGNORECASE)
    interesting_videos = df[df['Title'].apply(lambda x: bool(pattern.search(x)))]

    if interesting_videos.empty:
        fig, ax = plt.subplots()
        return ax

    interesting_videos = interesting_videos.copy()  # Create a copy to avoid modifying the input df
    interesting_videos['Like Ratio'] = interesting_videos['Likes'] / interesting_videos['Views']

    ax = interesting_videos.plot(kind='bar', x='Title', y='Like Ratio', legend=False)
    ax.set_ylabel('Like Ratio')
    ax.set_xticklabels(interesting_videos['Title'], rotation='vertical')

    return ax
