# Monty + pydeno gallery

Each example pairs Python running in [Monty](https://github.com/pydantic/monty) (the data half) with
JavaScript running in pydeno (the drawing half). Neither can reach your files, network or
environment. The pictures below are the actual outputs of the scripts in
[`examples/`](https://github.com/bmsuisse/pydeno/tree/main/examples), run from a checkout; the 3D
models are rendered here from the exported `.glb` files with three.js. The root `README.md` has the
table of what each half does.

## 3D models (three.js, exported as `.glb`)

![A green island with instanced pine trees and grey rock on steep slopes, on a sand-coloured plane](../assets/examples/monty_three_terrain.png)

`examples/monty_three_terrain.py`: a procedural island. Monty makes the heightmap and tree placement; three.js builds the mesh, paints it by height and slope and instances the trees.

![Speed-coloured orbit trails with three bodies in a figure-eight and longer trails reaching out](../assets/examples/monty_three_orbits.png)

`examples/monty_three_orbits.py`: an N-body simulation. Monty integrates the orbits; three.js turns each trail into a speed-coloured tube.

![A mountain range rising along the boundary of the Julia set, coloured by escape time](../assets/examples/monty_three_julia.png)

`examples/monty_three_julia.py`: escape-time counts from Monty's own `julia` example, extruded into a relief along the fractal's boundary.

![A grid of extruded buildings, lit ones in yellow and shaded ones dark](../assets/examples/monty_three_city.png)

`examples/monty_three_city.py`: a procedural city. Monty lays it out; three.js casts a ray from every roof to the sun and darkens the buildings that are shaded.

## Charts and maps (SVG and HTML)

![A force-directed dependency network with Voronoi cells and a treemap inset](../assets/examples/monty_d3_network.png)

`examples/monty_d3_network.py`: a package dependency graph. Monty computes PageRank and components; d3 runs the force layout and draws it as one [self-contained SVG](../assets/examples/monty_d3_network.svg).

![A four-panel dashboard: daily metric with flagged anomalies, weekly stacked totals, a correlation heatmap and a share donut](../assets/examples/monty_echarts_dashboard.png)

`examples/monty_echarts_dashboard.py`: a year of regional metrics. Monty finds the anomalies, correlations and trend; ECharts draws the four panels server-side into an HTML page with inline SVG.

![A map of vehicle tracks with buffered corridors, convex hulls, service zones and depots](../assets/examples/monty_turf_geo.png)

`examples/monty_turf_geo.py`: fleet GPS tracks. Monty cleans and resamples them; turf.js buffers, hulls and partitions them into service zones ([SVG](../assets/examples/monty_turf_geo.svg)).

![Revenue by month as a bar chart](../assets/examples/revenue_by_month.svg)

![Top customers by region as stacked bars](../assets/examples/top_customers_by_region.svg)

![A cohort retention heatmap](../assets/examples/cohort_retention.svg)

`examples/monty_sql_charts.py`: a business question answered through a read-only SQL tool, then drawn with Vega-Lite (revenue by month, top customers by region, cohort retention).
