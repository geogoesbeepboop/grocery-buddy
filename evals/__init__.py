"""Model-accuracy evals (run on-demand / nightly against the REAL models).

This is the *prompt-regression* tier of the two-tier eval design (see docs/EVALS.md):
it makes paid, nondeterministic API calls and scores the quality of Claude's outputs
(intent routing, briefing groundedness, synthesis, onboarding extraction). It is NOT
run per-commit — the deterministic, mocked code-path tests in tests/ are. Run with:

    uv run python -m evals.run                  # all suites
    uv run python -m evals.run --suite intents
"""
