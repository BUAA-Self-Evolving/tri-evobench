from random import randint, seed
import matplotlib.pyplot as plt
import pandas as pd
# Constants (they can be overridden with default parameters)
TEAMS = ['Team A', 'Team B', 'Team C', 'Team D', 'Team E']
PENALTY_COST = 1000  # in dollars
def task_func(goals, penalties, teams=TEAMS, penalty_cost=PENALTY_COST, rng_seed=None):
    if rng_seed is not None:
        seed(rng_seed)

    # Ensure goals and penalties are treated as positive
    goals = abs(goals)
    penalties = abs(penalties)

    match_results = []
    for team in teams:
        team_goals = randint(0, goals)
        team_penalties = randint(0, penalties)
        team_penalty_cost = penalty_cost * team_penalties
        match_results.append([team, team_goals, team_penalty_cost])

    results_df = pd.DataFrame(match_results, columns=['Team', 'Goals', 'Penalty Cost'])
    ax = results_df.plot(kind='bar', x='Team', y=['Goals', 'Penalty Cost'], stacked=True)
    plt.ylabel('Results')

    return results_df, ax