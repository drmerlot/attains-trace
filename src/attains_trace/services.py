"""Thin clients for the two public ArcGIS REST services the tracer reads.

- USGS Watershed Boundary Dataset, HUC12 layer (The National Map): every HUC12 with its downstream neighbor
  (`tohuc`), so the network can be walked downstream or upstream.
- EPA ATTAINS geospatial service: assessment units (points, lines, areas), the national assessment-unit-to-HUC12
  crosswalk, and the parameters (causes) behind each listing.

Queries are batched (`IN (...)` lists), paged past the server's record limit, retried, and optionally cached on disk
so repeated traces don't hit the services again.
"""
import hashlib
import json
import os
import time

import geopandas as gpd
import pandas as pd
import requests

WBD_HUC12 = "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer/6"
ATTAINS = "https://gispub.epa.gov/arcgis/rest/services/OW/ATTAINS_Assessment/MapServer"
ATTAINS_LAYERS = {"points": 0, "lines": 1, "areas": 2}
ATTAINS_BY_HUC12 = 7
ATTAINS_PARAMETERS = 8


class Client:
    """GET/POST to ArcGIS REST query endpoints with retries, paging and an optional JSON cache directory."""

    def __init__(self, cache_dir=None, timeout=120, retries=4, session=None):
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.retries = retries
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "attains-trace (https://github.com/drmerlot/attains-trace)")

    def _cache_path(self, url, params):
        if not self.cache_dir:
            return None
        key = hashlib.sha1((url + json.dumps(params, sort_keys=True)).encode()).hexdigest()
        return os.path.join(self.cache_dir, key + ".json")

    def get(self, url, params):
        path = self._cache_path(url, params)
        if path and os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        for attempt in range(self.retries):
            try:
                r = self.session.post(url, data=params, timeout=self.timeout)
                r.raise_for_status()
                data = r.json()
                if "error" in data:
                    raise RuntimeError(f"{url}: {data['error']}")
                break
            except (requests.RequestException, RuntimeError, ValueError):
                if attempt == self.retries - 1:
                    raise
                time.sleep(2 * (attempt + 1))
        if path:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(path, "w") as f:
                json.dump(data, f)
        return data

    def query(self, layer_url, where, fields="*", geometry=False, page=2000, fmt="json", order_by="OBJECTID"):
        """All features matching `where`, paging past the server's record limit. Returns the list of features
        (ArcGIS JSON) or, with fmt="geojson", a GeoJSON FeatureCollection."""
        out, offset = [], 0
        while True:
            params = {"where": where, "outFields": fields, "returnGeometry": str(geometry).lower(), "resultOffset": offset,
                      "resultRecordCount": page, "orderByFields": order_by, "f": fmt}
            if geometry:
                params["outSR"] = 4326
            data = self.get(f"{layer_url}/query", params)
            feats = data.get("features", [])
            out += feats
            if len(feats) < page and not data.get("exceededTransferLimit"):
                break
            offset += len(feats)
        return {"type": "FeatureCollection", "features": out} if fmt == "geojson" else out


def _in(field, values, quote=True):
    vals = ",".join(f"'{v}'" if quote else str(v) for v in values)
    return f"{field} IN ({vals})"


def _batched(values, n=100):
    values = list(dict.fromkeys(values))
    for i in range(0, len(values), n):
        yield values[i:i + n]


def huc12_records(client, codes):
    """WBD attributes for HUC12 codes: huc12, name, tohuc, states, areasqkm."""
    rows = []
    for batch in _batched(codes):
        rows += [f["attributes"] for f in client.query(WBD_HUC12, _in("huc12", batch), "huc12,name,tohuc,states,areasqkm", order_by="huc12")]
    return pd.DataFrame(rows, columns=["huc12", "name", "tohuc", "states", "areasqkm"])


def huc12_upstream_neighbors(client, codes):
    """HUC12s whose downstream neighbor is one of `codes`."""
    rows = []
    for batch in _batched(codes):
        rows += [f["attributes"] for f in client.query(WBD_HUC12, _in("tohuc", batch), "huc12,name,tohuc,states,areasqkm", order_by="huc12")]
    return pd.DataFrame(rows, columns=["huc12", "name", "tohuc", "states", "areasqkm"])


def huc12_polygons(client, codes):
    """HUC12 boundaries as a GeoDataFrame (EPSG:4326)."""
    frames = []
    for batch in _batched(codes, 50):
        fc = client.query(WBD_HUC12, _in("huc12", batch), "huc12,name,tohuc", geometry=True, fmt="geojson", page=500, order_by="huc12")
        if fc["features"]:
            frames.append(gpd.GeoDataFrame.from_features(fc, crs="EPSG:4326"))
    return pd.concat(frames, ignore_index=True) if frames else gpd.GeoDataFrame(columns=["huc12", "name", "tohuc", "geometry"], crs="EPSG:4326")


ASSESSMENT_FIELDS = ("huc12,assessmentunitidentifier,organizationid,reportingcycle,ircategory,overallstatus,isassessed,"
                     "isimpaired,on303dlist,hastmdl,has4bplan,waterbodyreportlink")


def assessments_by_huc12(client, codes):
    """EPA's assessment-unit-to-HUC12 crosswalk with each unit's listing status (latest cycle per unit)."""
    rows = []
    for batch in _batched(codes):
        rows += [f["attributes"] for f in client.query(f"{ATTAINS}/{ATTAINS_BY_HUC12}", _in("huc12", batch), ASSESSMENT_FIELDS)]
    df = pd.DataFrame(rows, columns=ASSESSMENT_FIELDS.split(","))
    if len(df):
        df = df.sort_values("reportingcycle").drop_duplicates(["huc12", "assessmentunitidentifier"], keep="last")
    return df.reset_index(drop=True)


def parameters(client, au_ids):
    """Parameters (pollutants and causes) assessed for each unit: name, group, status and category."""
    rows = []
    for batch in _batched(au_ids):
        rows += [f["attributes"] for f in client.query(f"{ATTAINS}/{ATTAINS_PARAMETERS}", _in("assessmentunitidentifier", batch),
                                                        "assessmentunitidentifier,parametername,parametergroupname,parameterstatus,parametercategorycode")]
    return pd.DataFrame(rows, columns=["assessmentunitidentifier", "parametername", "parametergroupname", "parameterstatus", "parametercategorycode"])


def assessment_geometries(client, au_ids):
    """Assessment-unit geometries (points, lines and areas) as one GeoDataFrame (EPSG:4326)."""
    frames = []
    for kind, layer in ATTAINS_LAYERS.items():
        for batch in _batched(au_ids, 50):
            fc = client.query(f"{ATTAINS}/{layer}", _in("assessmentunitidentifier", batch),
                              "assessmentunitidentifier,assessmentunitname,ircategory,isimpaired,hastmdl,overallstatus",
                              geometry=True, fmt="geojson", page=500)
            if fc["features"]:
                g = gpd.GeoDataFrame.from_features(fc, crs="EPSG:4326")
                g["kind"] = kind
                frames.append(g)
    return pd.concat(frames, ignore_index=True) if frames else gpd.GeoDataFrame(columns=["assessmentunitidentifier", "geometry"], crs="EPSG:4326")
