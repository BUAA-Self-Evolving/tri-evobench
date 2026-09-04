import unittest
# Unit Tests
class TestCases(unittest.TestCase):
    def test_no_goals_no_penalties(self):
        goals, penalties = {}, {}
        expected = pd.DataFrame({'Team': TEAMS, 'Score': [0] * 5})
        pd.testing.assert_frame_equal(task_func(goals, penalties), expected)
    def test_goals_no_penalties(self):
        goals = {team: index for index, team in enumerate(TEAMS, start=1)}
        penalties = {}
        expected = pd.DataFrame({'Team': TEAMS, 'Score': [1, 2, 3, 4, 5]})
        pd.testing.assert_frame_equal(task_func(goals, penalties), expected)
    def test_goals_with_penalties(self):
        goals = {team: 5 for team in TEAMS}
        penalties = {team: 2 for team in TEAMS}
        expected = pd.DataFrame({'Team': TEAMS, 'Score': [3] * 5})
        pd.testing.assert_frame_equal(task_func(goals, penalties), expected)
    def test_clipping_negative_scores(self):
        goals = {team: -15 for team in TEAMS}
        penalties = {team: 0 for team in TEAMS}
        expected = pd.DataFrame({'Team': TEAMS, 'Score': [-10] * 5})
        pd.testing.assert_frame_equal(task_func(goals, penalties), expected)
    def test_clipping_positive_scores(self):
        goals = {team: 20 for team in TEAMS}
        penalties = {team: 0 for team in TEAMS}
        expected = pd.DataFrame({'Team': TEAMS, 'Score': [10] * 5})
        pd.testing.assert_frame_equal(task_func(goals, penalties), expected)