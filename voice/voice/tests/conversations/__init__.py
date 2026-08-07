"""Full multi-turn conversation threads against the shared agent brain.

Each ``test_thread_*.py`` module plays ONE realistic caller from hello to goodbye and asserts
what the agent did at every turn — the route it took, the tools it fired, the slots it derived,
and whether the answer was grounded. The unit tests next door prove each tool in isolation;
these prove the conversation.
"""
