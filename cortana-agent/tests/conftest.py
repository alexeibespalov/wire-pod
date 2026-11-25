"""Tests run without network or keys: data in a temp dir, no satellite, a dummy API key."""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp(prefix="cortana-test-"))
os.environ.setdefault("LLM_PROVIDER", "anthropic")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ["SATELLITE_URI"] = ""
os.environ["YTMUSIC_ENABLED"] = "0"        # tests that want YouTube Music hand in a fake one
os.environ.pop("TAVILY_API_KEY", None)
os.environ.pop("HA_TOKEN", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
