import pytest
import requests


def test_external_network_blocked() -> None:
    with pytest.raises(Exception, match="must not use the network"):
        requests.get("https://example.com", timeout=2)
