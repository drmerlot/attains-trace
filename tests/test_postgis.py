"""PostGIS mirror on the synthetic network: recursive SQL equals the Python tracer, point lookup uses the GiST index.
Runs when ATTAINS_TRACE_PG points at a PostGIS database (CI starts one as a service container)."""
import os

import geopandas as gpd
import pytest
from shapely.geometry import box

from attains_trace import trace
from test_trace import ASSESS, NODES, PARAMS, FakeNetwork

DSN = os.environ.get("ATTAINS_TRACE_PG")
pytestmark = pytest.mark.skipif(not DSN, reason="set ATTAINS_TRACE_PG to a PostGIS connection string")

# unit squares laid out left to right
BOXES = {code: box(i, 0, i + 1, 1) for i, code in enumerate(sorted(NODES))}


@pytest.fixture
def conn():
    from attains_trace import postgis
    c = postgis.connect(DSN)
    with c.cursor() as cur:        # a throwaway schema, so a real mirror in the same database is never touched
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis; DROP SCHEMA IF EXISTS attains_trace_test CASCADE; "
                    "CREATE SCHEMA attains_trace_test; SET search_path TO attains_trace_test, public")
    c.commit()
    g = gpd.GeoDataFrame([{"huc12": k, "name": v[0], "tohuc": v[1], "geometry": BOXES[k]} for k, v in NODES.items()], crs="EPSG:4326")
    a = ASSESS.assign(organizationid="TEST", reportingcycle=2026, ircategory="5", overallstatus="Not Supporting", isassessed="Y",
                      on303dlist="Y", has4bplan="N", waterbodyreportlink="")
    postgis.load_frames(c, g, a, PARAMS)
    yield c
    c.rollback()
    with c.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS attains_trace_test CASCADE")
    c.commit()
    c.close()


def test_recursive_sql_matches_python(conn):
    from attains_trace import postgis
    down = postgis.trace_downstream_sql(conn, "000000000001")
    assert down["name"].tolist() == ["A", "C", "E"]
    up = postgis.trace_upstream_sql(conn, "000000000005")
    assert dict(zip(up["name"], up["hops"])) == {"E": 0, "C": 1, "D": 1, "A": 2, "B": 2}
    for direction, start in (("downstream", "000000000001"), ("upstream", "000000000005")):
        db = trace(start, direction, postgis.PostgisNetwork(conn))
        py = trace(start, direction, FakeNetwork())
        assert sorted(db.path["huc12"]) == sorted(py.path["huc12"])
        assert sorted(db.impaired["assessmentunitidentifier"]) == sorted(py.impaired["assessmentunitidentifier"])
        assert db.cause_summary()["with_tmdl"].sum() == py.cause_summary()["with_tmdl"].sum()


def test_point_lookup_and_index(conn):
    from attains_trace import postgis
    assert postgis.huc12_at(conn, 2.5, 0.5) == sorted(NODES)[2]
    assert postgis.huc12_at(conn, 50, 50) is None
    with conn.cursor() as cur:
        cur.execute("SET enable_seqscan = off")      # tiny table: make the planner show it can use the GiST index
    plan = postgis.explain(conn, postgis.POINT_SQL, {"lon": 2.5, "lat": 0.5})
    assert "huc12_geom_idx" in plan


def test_upsert_is_idempotent(conn):
    from attains_trace import postgis
    g = gpd.GeoDataFrame([{"huc12": "000000000001", "name": "A renamed", "tohuc": "000000000003", "geometry": BOXES["000000000001"]}], crs="EPSG:4326")
    postgis.load_frames(conn, g)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), max(name) FILTER (WHERE huc12 = '000000000001') FROM huc12")
        assert cur.fetchone() == (5, "A renamed")
