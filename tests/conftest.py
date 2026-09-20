"""Shared fixtures for the AlarmSystem integration tests."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let the test Home Assistant load custom_components/alarmsystem."""
    yield
