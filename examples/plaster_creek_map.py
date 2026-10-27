"""Trace downstream from Headwaters Plaster Creek (Gaines Township, Michigan) to Lake Michigan and map the impaired
waters along the way. Writes docs/plaster_creek_trace.png."""
import matplotlib.pyplot as plt

from attains_trace import ServiceNetwork, trace

net = ServiceNetwork(cache_dir=".cache")
t = trace("040500060505", "downstream", net)
polys, units = t.to_geodataframes(net)
# some units are large (a Great Lakes shoreline segment, a whole lake); show only the part inside the traced HUC12s
units = units.clip(polys.union_all())
impaired = units[units["isimpaired"] == "Y"]
other = units[units["isimpaired"] != "Y"]

fig, ax = plt.subplots(figsize=(9, 6.5))
polys.plot(ax=ax, column="hops", cmap="Blues", alpha=0.45, edgecolor="#4b5563", linewidth=0.6, legend=True,
           legend_kwds={"label": "hops downstream from the start", "shrink": 0.6})
other.plot(ax=ax, color="#9ca3af", linewidth=1.0, markersize=8)
impaired.plot(ax=ax, color="#c2410c", linewidth=1.8, markersize=14)
polys[polys["hops"] == 0].boundary.plot(ax=ax, color="black", linewidth=1.6)
for hop, label in ((0, "start: Headwaters Plaster Creek"), (int(polys["hops"].max()), "to Lake Michigan")):
    p = polys[polys["hops"] == hop].geometry.iloc[0].representative_point()
    ax.annotate(label, (p.x, p.y), xytext=(0, -18 if hop == 0 else 14), textcoords="offset points", fontsize=9, ha="center")
ax.set_title("Downstream from Plaster Creek, Michigan, to Lake Michigan\nimpaired waters (orange), other assessed waters (gray)", fontsize=11)
ax.set_axis_off()
fig.tight_layout()
fig.savefig("docs/plaster_creek_trace.png", dpi=150)
print(f"{len(t.path)} HUC12s, {units['assessmentunitidentifier'].nunique()} assessed waters mapped, {impaired['assessmentunitidentifier'].nunique()} impaired")
