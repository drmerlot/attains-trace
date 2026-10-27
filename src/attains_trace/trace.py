"""Trace a watershed network downstream or upstream from a HUC12 and collect the assessed and impaired waters.

The walk needs only two things from a network, so the same tracer runs against the live web services
(`ServiceNetwork`), a PostGIS mirror (`attains_trace.postgis.PostgisNetwork`) or a test fake:

    records(codes) -> DataFrame[huc12, name, tohuc, ...]      the HUC12s themselves
    upstream(codes) -> DataFrame[huc12, name, tohuc, ...]     HUC12s that drain into any of `codes`

and, for the water-quality side, assessments(codes) and parameters(au_ids) as in `attains_trace.services`.
"""
from dataclasses import dataclass, field

import pandas as pd

from . import services

# WBD `tohuc` values that end a downstream walk
TERMINALS = {"OCEAN", "CLOSED BASIN", "CANADA", "MEXICO", "GREAT LAKES", "", None}


class ServiceNetwork:
    """The national network read live from the USGS and EPA services (optionally cached on disk)."""

    def __init__(self, cache_dir=None, client=None):
        self.client = client or services.Client(cache_dir=cache_dir)

    def records(self, codes):
        return services.huc12_records(self.client, codes)

    def upstream(self, codes):
        return services.huc12_upstream_neighbors(self.client, codes)

    def assessments(self, codes):
        return services.assessments_by_huc12(self.client, codes)

    def parameters(self, au_ids):
        return services.parameters(self.client, au_ids)

    def huc12_polygons(self, codes):
        return services.huc12_polygons(self.client, codes)

    def assessment_geometries(self, au_ids):
        return services.assessment_geometries(self.client, au_ids)


@dataclass
class Trace:
    """Result of a trace. `path` lists the HUC12s in order with their distance (hops) from the start; `waters`
    lists the assessment units in them with listing status; `causes` lists the impairment causes by unit."""
    start: str
    direction: str
    path: pd.DataFrame
    waters: pd.DataFrame
    causes: pd.DataFrame
    truncated: bool = False
    notes: list = field(default_factory=list)

    @property
    def impaired(self):
        """Impaired units (EPA category 4 or 5), nearest first."""
        w = self.waters
        return w[w["isimpaired"] == "Y"].sort_values(["hops", "assessmentunitidentifier"]).reset_index(drop=True)

    def cause_summary(self):
        """Each impairment cause group with the number of impaired units listing it, how many of those have a TMDL
        for that cause (EPA category 4A for the parameter), and the nearest hop it appears at."""
        cols = ["parametergroupname", "units", "with_tmdl", "nearest_hop"]
        if self.causes.empty:
            return pd.DataFrame(columns=cols)
        c = self.causes.merge(self.impaired[["assessmentunitidentifier", "hops"]].drop_duplicates("assessmentunitidentifier"),
                              on="assessmentunitidentifier")
        c["tmdl"] = c["parametercategorycode"].astype(str).str.upper().str.startswith("4A")
        per_unit = c.groupby(["parametergroupname", "assessmentunitidentifier"]).agg(tmdl=("tmdl", "any"), hops=("hops", "min")).reset_index()
        g = per_unit.groupby("parametergroupname").agg(units=("assessmentunitidentifier", "nunique"), with_tmdl=("tmdl", "sum"),
                                                       nearest_hop=("hops", "min"))
        g["with_tmdl"] = g["with_tmdl"].astype(int)
        return g.sort_values(["nearest_hop", "units"], ascending=[True, False]).reset_index()[cols]

    def to_geodataframes(self, network):
        """HUC12 polygons along the trace and the assessment-unit geometries, as two GeoDataFrames."""
        polys = network.huc12_polygons(self.path["huc12"].tolist()).merge(self.path[["huc12", "hops"]], on="huc12")
        units = network.assessment_geometries(self.waters["assessmentunitidentifier"].unique().tolist())
        if len(units):
            units = units.merge(self.waters[["assessmentunitidentifier", "huc12", "hops"]].drop_duplicates("assessmentunitidentifier"),
                                on="assessmentunitidentifier", how="left")
        return polys, units


def downstream_path(network, start, max_hops=100):
    """HUC12s from `start` to the network's outlet (or `max_hops`), following `tohuc`."""
    rows, code, seen = [], start, set()
    for hop in range(max_hops + 1):
        rec = network.records([code])
        if rec.empty:
            break
        r = rec.iloc[0].to_dict()
        rows.append({**r, "hops": hop})
        seen.add(code)
        nxt = r.get("tohuc")
        if nxt in TERMINALS or not isinstance(nxt, str) or len(nxt) != 12 or nxt in seen:
            break
        code = nxt
    truncated = len(rows) == max_hops + 1
    return pd.DataFrame(rows), truncated


def upstream_network(network, start, max_hops=50, max_units=2000):
    """HUC12s draining to `start`, breadth-first by hops, stopping at `max_hops` or `max_units`."""
    first = network.records([start])
    if first.empty:
        return first.assign(hops=pd.Series(dtype=int)), False
    frames = [first.assign(hops=0)]
    seen, frontier, total = {start}, [start], 1
    truncated = False
    for hop in range(1, max_hops + 1):
        up = network.upstream(frontier)
        up = up[~up["huc12"].isin(seen)]
        if up.empty:
            break
        if total + len(up) > max_units:
            up = up.iloc[: max_units - total]
            truncated = True
        frames.append(up.assign(hops=hop))
        seen.update(up["huc12"])
        frontier = up["huc12"].tolist()
        total += len(up)
        if truncated:
            break
    else:
        truncated = bool(len(network.upstream(frontier)))
    return pd.concat(frames, ignore_index=True), truncated


def trace(start, direction="downstream", network=None, max_hops=None, max_units=2000):
    """Walk the HUC12 network from `start` and collect the assessed waters along the way.

    direction: "downstream" (to the outlet) or "upstream" (everything draining to `start`, capped).
    """
    if direction not in ("downstream", "upstream"):
        raise ValueError("direction must be 'downstream' or 'upstream'")
    network = network or ServiceNetwork()
    if direction == "downstream":
        path, truncated = downstream_path(network, start, max_hops or 100)
    else:
        path, truncated = upstream_network(network, start, max_hops or 50, max_units)
    notes = []
    if path.empty:
        notes.append(f"{start} is not in the HUC12 network")
        empty = pd.DataFrame(columns=["huc12", "assessmentunitidentifier", "hops", "isimpaired", "hastmdl"])
        return Trace(start, direction, path, empty, pd.DataFrame(columns=["assessmentunitidentifier", "parametergroupname"]), truncated, notes)
    waters = network.assessments(path["huc12"].tolist())
    waters = waters.merge(path[["huc12", "hops"]], on="huc12", how="left").sort_values(["hops", "assessmentunitidentifier"]).reset_index(drop=True)
    impaired_ids = waters.loc[waters["isimpaired"] == "Y", "assessmentunitidentifier"].unique().tolist()
    causes = network.parameters(impaired_ids) if impaired_ids else pd.DataFrame(columns=["assessmentunitidentifier", "parametergroupname", "parameterstatus"])
    if len(causes):
        causes = causes[causes["parameterstatus"].astype(str).str.lower() == "cause"].drop_duplicates(
            ["assessmentunitidentifier", "parametername"]).reset_index(drop=True)
    if truncated:
        notes.append("trace stopped at the hop or unit limit; raise max_hops or max_units to go further")
    return Trace(start, direction, path.reset_index(drop=True), waters, causes, truncated, notes)
