"""Tracing logic on a small synthetic network (no network access needed).

        A   B
         \\ /
          C   D
           \\ /
            E -> OCEAN
"""
import pandas as pd
import pytest

from attains_trace import downstream_path, trace, upstream_network

NODES = {  # huc12: (name, tohuc)
    "000000000001": ("A", "000000000003"), "000000000002": ("B", "000000000003"),
    "000000000003": ("C", "000000000005"), "000000000004": ("D", "000000000005"),
    "000000000005": ("E", "OCEAN"),
}
ASSESS = pd.DataFrame([
    # huc12, unit, impaired, tmdl
    ("000000000001", "AU-A1", "Y", "N"), ("000000000003", "AU-C1", "Y", "Y"), ("000000000003", "AU-C2", "N", "N"),
    ("000000000005", "AU-E1", "Y", "Y"), ("000000000004", "AU-D1", "Y", "N"),
], columns=["huc12", "assessmentunitidentifier", "isimpaired", "hastmdl"])
PARAMS = pd.DataFrame([
    ("AU-A1", "E. COLI", "PATHOGENS", "Cause", "5"), ("AU-C1", "E. COLI", "PATHOGENS", "Cause", "4A"),
    ("AU-C1", "PHOSPHORUS", "NUTRIENTS", "Cause", "5"), ("AU-C1", "DISSOLVED OXYGEN", "ORGANIC ENRICHMENT", "Meeting Criteria", "1"),
    ("AU-E1", "PCB IN FISH TISSUE", "POLYCHLORINATED BIPHENYLS (PCBS)", "Cause", "4A"), ("AU-D1", "SEDIMENT", "SEDIMENT", "Cause", "5"),
], columns=["assessmentunitidentifier", "parametername", "parametergroupname", "parameterstatus", "parametercategorycode"])


class FakeNetwork:
    def __init__(self):
        self.calls = 0

    def _df(self, codes):
        return pd.DataFrame([(c, NODES[c][0], NODES[c][1]) for c in codes], columns=["huc12", "name", "tohuc"])

    def records(self, codes):
        self.calls += 1
        return self._df([c for c in codes if c in NODES])

    def upstream(self, codes):
        self.calls += 1
        return self._df(sorted(c for c, (_, to) in NODES.items() if to in codes))

    def assessments(self, codes):
        return ASSESS[ASSESS["huc12"].isin(codes)].copy()

    def parameters(self, au_ids):
        return PARAMS[PARAMS["assessmentunitidentifier"].isin(au_ids)].copy()


def test_downstream_path_to_outlet():
    path, truncated = downstream_path(FakeNetwork(), "000000000001")
    assert path["name"].tolist() == ["A", "C", "E"] and path["hops"].tolist() == [0, 1, 2] and not truncated


def test_downstream_hop_limit():
    path, truncated = downstream_path(FakeNetwork(), "000000000001", max_hops=1)
    assert path["name"].tolist() == ["A", "C"] and truncated


def test_upstream_network():
    path, truncated = upstream_network(FakeNetwork(), "000000000005")
    assert dict(zip(path["name"], path["hops"])) == {"E": 0, "C": 1, "D": 1, "A": 2, "B": 2} and not truncated
    capped, truncated = upstream_network(FakeNetwork(), "000000000005", max_units=3)
    assert len(capped) == 3 and truncated


def test_trace_downstream_waters_and_causes():
    t = trace("000000000001", "downstream", FakeNetwork())
    assert t.impaired["assessmentunitidentifier"].tolist() == ["AU-A1", "AU-C1", "AU-E1"]
    assert "AU-D1" not in set(t.waters["assessmentunitidentifier"])        # D is not downstream of A
    s = t.cause_summary().set_index("parametergroupname")
    assert s.loc["PATHOGENS", "units"] == 2 and s.loc["PATHOGENS", "with_tmdl"] == 1 and s.loc["PATHOGENS", "nearest_hop"] == 0
    assert s.loc["NUTRIENTS", "with_tmdl"] == 0
    assert "ORGANIC ENRICHMENT" not in s.index                                # meeting criteria is not a cause


def test_trace_upstream_includes_tributaries():
    t = trace("000000000005", "upstream", FakeNetwork())
    assert set(t.impaired["assessmentunitidentifier"]) == {"AU-A1", "AU-C1", "AU-D1", "AU-E1"}


def test_unknown_start_and_bad_direction():
    t = trace("999999999999", "downstream", FakeNetwork())
    assert t.path.empty and t.notes
    with pytest.raises(ValueError):
        trace("000000000001", "sideways", FakeNetwork())
