"""Cross-channel answer evals — one golden set, one adapter per channel, one score.

``golden.py`` loads ``kb/golden/answers.yaml`` and resolves its ``{{…}}`` fact templates against
the live KB (so the golden file never carries an hour, a price, or a deal literal — the KB row is
the only source). ``adapters.py`` gives every channel the same ``ask()`` shape. ``score.py`` turns
answers into pass/fail per check and a cross-channel consistency count. ``manage.py eval_answers``
and ``voice/tests/test_eval_answers.py`` are the two front doors.
"""
