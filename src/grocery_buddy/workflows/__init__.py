# Keep this file empty.
#
# Temporal's sandbox imports the *package* __init__ before importing the
# workflow module. Any import here that transitively calls non-deterministic
# stdlib (pathlib, os, random, datetime.now, etc.) will fail sandbox validation.
#
# Import directly from submodules instead:
#   from grocery_buddy.workflows.grocery_run import GroceryRunWorkflow
#   from grocery_buddy.workflows.activities import load_user_data, ...
