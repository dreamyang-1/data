"""Test-infrastructure isolation from the developer machine ``.env``.

``app.config.Settings`` merges ``env_file=(PLATFORM_ENV, SHARED_PROJECT_ENV,
PROJECT_ROOT / ".env")``, so a developer ``.env`` that selects
``DATA_AGENT_RUNTIME_MODE=V2_CONTEXT_V1_EXECUTION`` leaks into every test that
imports the application, even though offline suites assume the V1 default.

Pin the *default* runtime mode to V1 before any test module imports the app.
pydantic-settings resolves init arguments before process environment variables
and before dotenv files, so this hook cannot weaken tests that explicitly
select V2 (``Settings(runtime_mode="V2_CONTEXT_V1_EXECUTION", ...)``), and
``setdefault`` preserves an operator-supplied ``DATA_AGENT_RUNTIME_MODE``
process export.  This only affects the pytest process, never the production
default.
"""

import os

os.environ.setdefault("DATA_AGENT_RUNTIME_MODE", "V1")
