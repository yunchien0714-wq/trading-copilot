import os

# The tests exercise the engine's built-in rule set; config/strategy.toml (the trader's own strategy) is ignored here.
os.environ["COPILOT_STRATEGY"] = "builtin"
