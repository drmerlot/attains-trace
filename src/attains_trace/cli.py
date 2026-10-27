"""Command line: trace a HUC12 and print the impaired waters along the way.

    attains-trace 040500060505                     downstream from Headwaters Plaster Creek, Michigan
    attains-trace 040500060512 --upstream --max-hops 10
    attains-trace 040500060505 --geojson trace     writes trace_huc12.geojson and trace_waters.geojson
    attains-trace 040500060505 --postgis postgresql://...   trace against a PostGIS mirror instead of the services
"""
import argparse
import sys

from .trace import ServiceNetwork, trace


def main(argv=None):
    a = argparse.ArgumentParser(prog="attains-trace", description="Trace a HUC12 and list impaired waters, causes and TMDLs.")
    a.add_argument("huc12", help="12-digit HUC to start from")
    a.add_argument("--upstream", action="store_true", help="trace everything draining to the HUC12 instead of downstream")
    a.add_argument("--max-hops", type=int, default=None)
    a.add_argument("--max-units", type=int, default=2000, help="upstream cap on HUC12s")
    a.add_argument("--cache", default=None, help="directory to cache service responses")
    a.add_argument("--postgis", default=None, help="PostgreSQL connection string for a PostGIS mirror")
    a.add_argument("--geojson", default=None, help="write <prefix>_huc12.geojson and <prefix>_waters.geojson")
    o = a.parse_args(argv)
    if o.postgis:
        from .postgis import PostgisNetwork, connect
        network = PostgisNetwork(connect(o.postgis))
    else:
        network = ServiceNetwork(cache_dir=o.cache)
    t = trace(o.huc12, "upstream" if o.upstream else "downstream", network, o.max_hops, o.max_units)
    for note in t.notes:
        print("note:", note, file=sys.stderr)
    if t.path.empty:
        return 1
    print(f"{t.direction} from {o.huc12}: {len(t.path)} HUC12s, {t.waters['assessmentunitidentifier'].nunique()} assessed waters, "
          f"{t.impaired['assessmentunitidentifier'].nunique()} impaired")
    if o.upstream:
        print(t.path.groupby("hops").size().rename("huc12s").to_string())
    else:
        print(t.path[["hops", "huc12", "name"]].to_string(index=False))
    summary = t.cause_summary()
    if len(summary):
        print("\nimpairment causes (units listing it, units with a TMDL for it, nearest hop):")
        print(summary.to_string(index=False))
    if o.geojson:
        polys, units = t.to_geodataframes(network)
        polys.to_file(f"{o.geojson}_huc12.geojson", driver="GeoJSON")
        if len(units):
            units.to_file(f"{o.geojson}_waters.geojson", driver="GeoJSON")
        print(f"\nwrote {o.geojson}_huc12.geojson and {o.geojson}_waters.geojson")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
