"""Against the live EPA and USGS services. Opt in with ATTAINS_TRACE_LIVE=1 (it makes a few dozen requests)."""
import os

import pytest

from attains_trace import ServiceNetwork, trace

pytestmark = pytest.mark.skipif(not os.environ.get("ATTAINS_TRACE_LIVE"), reason="set ATTAINS_TRACE_LIVE=1")


def test_plaster_creek_to_lake_michigan():
    t = trace("040500060505", "downstream", ServiceNetwork())
    assert t.path["huc12"].iloc[0] == "040500060505"
    assert t.path["name"].str.contains("Grand River").any()
    assert len(t.impaired) > 0 and len(t.cause_summary()) > 0
