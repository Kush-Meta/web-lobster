import pytest

from tests.sites import build_sites


@pytest.fixture
def sites():
    """Two local sites: A (allowed by the test mandate) and B (the attacker)."""
    a, b = build_sites()
    yield a, b
    a.close()
    b.close()
