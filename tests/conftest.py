"""Offline tests use explicit dummy configuration and never call real providers."""
import os

os.environ.setdefault("IVC_API_KEY", "test-key-not-a-real-credential")
os.environ.setdefault("IVC_LANGSMITH_TRACING", "false")
