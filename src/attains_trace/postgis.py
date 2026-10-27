"""A PostGIS mirror of the HUC12 network and the ATTAINS listings for a region, so traces run in the database.

Schema (one transaction, idempotent):
  huc12(huc12 PK, name, tohuc, states, areasqkm, geom MultiPolygon 4326)   B-tree on tohuc, GiST on geom
  assessment(huc12, assessmentunitidentifier, ..., PK (huc12, assessmentunitidentifier))   B-tree on the unit id
  parameter(assessmentunitidentifier, parametername, ...)                      B-tree on the unit id

Traces are single recursive queries (WITH RECURSIVE over huc12.tohuc), and point lookups (which HUC12 is this
site in?) use the GiST index through ST_Intersects. `PostgisNetwork` gives the same interface as the live
`ServiceNetwork`, so `attains_trace.trace(...)` runs unchanged against the database.
"""
import geopandas as gpd
import pandas as pd
import psycopg
from shapely import MultiPolygon, to_wkb

from . import services

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE TABLE IF NOT EXISTS huc12 (
    huc12    text PRIMARY KEY,
    name     text,
    tohuc    text,
    states   text,
    areasqkm double precision,
    geom     geometry(MultiPolygon, 4326)
);
CREATE INDEX IF NOT EXISTS huc12_tohuc_idx ON huc12 (tohuc);
CREATE INDEX IF NOT EXISTS huc12_geom_idx ON huc12 USING gist (geom);
CREATE TABLE IF NOT EXISTS assessment (
    huc12                    text NOT NULL,
    assessmentunitidentifier text NOT NULL,
    organizationid           text,
    reportingcycle           integer,
    ircategory               text,
    overallstatus            text,
    isassessed               text,
    isimpaired               text,
    on303dlist               text,
    hastmdl                  text,
    has4bplan                text,
    waterbodyreportlink      text,
    PRIMARY KEY (huc12, assessmentunitidentifier)
);
CREATE INDEX IF NOT EXISTS assessment_au_idx ON assessment (assessmentunitidentifier);
CREATE TABLE IF NOT EXISTS parameter (
    assessmentunitidentifier text NOT NULL,
    parametername            text,
    parametergroupname       text,
    parameterstatus          text,
    parametercategorycode    text
);
CREATE INDEX IF NOT EXISTS parameter_au_idx ON parameter (assessmentunitidentifier);
"""

DOWNSTREAM_SQL = """
WITH RECURSIVE down AS (
    SELECT huc12, name, tohuc, states, areasqkm, 0 AS hops, ARRAY[huc12] AS seen
    FROM huc12 WHERE huc12 = %(start)s
  UNION ALL
    SELECT h.huc12, h.name, h.tohuc, h.states, h.areasqkm, d.hops + 1, d.seen || h.huc12
    FROM down d JOIN huc12 h ON h.huc12 = d.tohuc
    WHERE d.hops < %(max_hops)s AND NOT h.huc12 = ANY (d.seen)
)
SELECT huc12, name, tohuc, states, areasqkm, hops FROM down ORDER BY hops
"""

UPSTREAM_SQL = """
WITH RECURSIVE up AS (
    SELECT huc12, name, tohuc, states, areasqkm, 0 AS hops FROM huc12 WHERE huc12 = %(start)s
  UNION
    SELECT h.huc12, h.name, h.tohuc, h.states, h.areasqkm, u.hops + 1
    FROM up u JOIN huc12 h ON h.tohuc = u.huc12
    WHERE u.hops < %(max_hops)s
)
SELECT huc12, min(name) AS name, min(tohuc) AS tohuc, min(states) AS states, min(areasqkm) AS areasqkm, min(hops) AS hops
FROM up GROUP BY huc12 ORDER BY hops, huc12
"""

POINT_SQL = """
SELECT huc12, name FROM huc12
WHERE ST_Intersects(geom, ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326))
"""


def connect(dsn):
    return psycopg.connect(dsn, autocommit=False)


def create_schema(conn):
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
    conn.commit()


def _multipolygon(geom):
    return geom if geom is None or geom.geom_type == "MultiPolygon" else MultiPolygon([geom])


def load_frames(conn, huc12s, assessments=None, parameters=None):
    """Upsert HUC12s (a GeoDataFrame with huc12, name, tohuc and optional states, areasqkm), assessments and
    parameters into the mirror."""
    create_schema(conn)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO huc12 (huc12, name, tohuc, states, areasqkm, geom) "
            "VALUES (%s, %s, %s, %s, %s, ST_GeomFromWKB(%s, 4326)) "
            "ON CONFLICT (huc12) DO UPDATE SET name = EXCLUDED.name, tohuc = EXCLUDED.tohuc, states = EXCLUDED.states, "
            "areasqkm = EXCLUDED.areasqkm, geom = EXCLUDED.geom",
            [(r.huc12, r.name, r.tohuc, getattr(r, "states", None), getattr(r, "areasqkm", None),
              to_wkb(_multipolygon(r.geometry)) if r.geometry is not None else None) for r in huc12s.itertuples()])
        if assessments is not None and len(assessments):
            cols = [c for c in services.ASSESSMENT_FIELDS.split(",")]
            cur.executemany(
                f"INSERT INTO assessment ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                "ON CONFLICT (huc12, assessmentunitidentifier) DO UPDATE SET "
                + ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in ("huc12", "assessmentunitidentifier")),
                [tuple(None if pd.isna(v) else v for v in row) for row in assessments[cols].itertuples(index=False)])
        if parameters is not None and len(parameters):
            ids = list(parameters["assessmentunitidentifier"].unique())
            cur.execute("DELETE FROM parameter WHERE assessmentunitidentifier = ANY (%s)", (ids,))
            pcols = ["assessmentunitidentifier", "parametername", "parametergroupname", "parameterstatus", "parametercategorycode"]
            cur.executemany(f"INSERT INTO parameter ({', '.join(pcols)}) VALUES (%s, %s, %s, %s, %s)",
                            [tuple(row) for row in parameters[pcols].itertuples(index=False)])
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("ANALYZE huc12; ANALYZE assessment; ANALYZE parameter;")
    conn.commit()


def load_region(conn, huc_prefix, client=None):
    """Mirror every HUC12 whose code starts with `huc_prefix` (for example a HUC6 or HUC8), with its boundaries,
    the ATTAINS units crosswalked to it and the parameters of the impaired ones, from the live services."""
    client = client or services.Client()
    feats = client.query(services.WBD_HUC12, f"huc12 LIKE '{huc_prefix}%'", "huc12,name,tohuc,states,areasqkm",
                         geometry=True, fmt="geojson", page=500, order_by="huc12")
    gdf = gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326")
    assess = services.assessments_by_huc12(client, gdf["huc12"].tolist())
    impaired = assess.loc[assess["isimpaired"] == "Y", "assessmentunitidentifier"].unique().tolist()
    params = services.parameters(client, impaired) if impaired else None
    load_frames(conn, gdf, assess, params)
    return len(gdf), len(assess), 0 if params is None else len(params)


def trace_downstream_sql(conn, start, max_hops=100):
    """The downstream path as one recursive query."""
    return pd.read_sql_query(DOWNSTREAM_SQL, conn, params={"start": start, "max_hops": max_hops})


def trace_upstream_sql(conn, start, max_hops=50):
    """Everything draining to `start` (within `max_hops`), as one recursive query."""
    return pd.read_sql_query(UPSTREAM_SQL, conn, params={"start": start, "max_hops": max_hops})


def huc12_at(conn, lon, lat):
    """The HUC12 containing a point (uses the GiST index)."""
    df = pd.read_sql_query(POINT_SQL, conn, params={"lon": lon, "lat": lat})
    return None if df.empty else df.iloc[0]["huc12"]


def explain(conn, sql, params):
    """EXPLAIN ANALYZE output for a query, to check that indexes are used."""
    with conn.cursor() as cur:
        cur.execute("EXPLAIN ANALYZE " + sql, params)
        return "\n".join(r[0] for r in cur.fetchall())


class PostgisNetwork:
    """The network interface the tracer uses, served from the PostGIS mirror."""

    _COLS = ["huc12", "name", "tohuc", "states", "areasqkm"]

    def __init__(self, conn):
        self.conn = conn

    def _q(self, sql, params):
        return pd.read_sql_query(sql, self.conn, params=params)

    def records(self, codes):
        return self._q("SELECT huc12, name, tohuc, states, areasqkm FROM huc12 WHERE huc12 = ANY (%(c)s)", {"c": list(codes)})

    def upstream(self, codes):
        return self._q("SELECT huc12, name, tohuc, states, areasqkm FROM huc12 WHERE tohuc = ANY (%(c)s) ORDER BY huc12", {"c": list(codes)})

    def assessments(self, codes):
        return self._q(f"SELECT {services.ASSESSMENT_FIELDS} FROM assessment WHERE huc12 = ANY (%(c)s)", {"c": list(codes)})

    def parameters(self, au_ids):
        return self._q("SELECT assessmentunitidentifier, parametername, parametergroupname, parameterstatus, parametercategorycode "
                       "FROM parameter WHERE assessmentunitidentifier = ANY (%(c)s)", {"c": list(au_ids)})

    def huc12_polygons(self, codes):
        return gpd.read_postgis("SELECT huc12, name, tohuc, geom FROM huc12 WHERE huc12 = ANY (%(c)s)", self.conn,
                                geom_col="geom", params={"c": list(codes)})
