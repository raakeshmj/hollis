import os

from hypothesis import settings

# CI runs derandomized so failures reproduce; locally Hypothesis explores new examples.
settings.register_profile("ci", derandomize=True, deadline=None, print_blob=True)
settings.register_profile("dev", deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
