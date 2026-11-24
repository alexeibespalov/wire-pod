"""agent-core: what Vector and Cortana have in common.

Each agent keeps its persona, tools, safety guard and front end, and registers its own
config module here before importing anything else from this package:

    # app/config.py
    from agent_core import settings
    settings.load_shared(globals(), provider_default="anthropic")
    ASSISTANT_NAME = "Cortana"; LOGGER_NAME = "cortana-agent"; ...
    settings.use(sys.modules[__name__])

Core modules read every setting through `settings.cfg`, which forwards to that module, so
there is one set of values per process (and tests can monkeypatch the agent's config).
"""
