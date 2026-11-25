import pytest


@pytest.fixture(autouse=True)
def _rules_extraction_by_default(monkeypatch):
    """Most tests exercise the rule-based reader; AI-extraction tests switch EXTRACTION back to 'ai'."""
    monkeypatch.setenv("EXTRACTION", "rules")
    monkeypatch.setenv("RECON_LOAD_DOTENV", "0")              # never read the real .env in tests
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for k in ("VIASOCKET_ORG_ID", "VIASOCKET_PROJECT_ID", "VIASOCKET_EMBED_SECRET", "DBDASH_AUTH_KEY", "STORAGE"):
        monkeypatch.delenv(k, raising=False)                  # tests never call the real viaSocket
