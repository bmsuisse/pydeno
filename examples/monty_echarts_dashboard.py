"""Analytics dashboard: Monty analyses the data, ECharts draws it, neither is trusted.

An agent asked for "a dashboard of last year's regional metrics, with anomalies flagged" writes two
programs, because each language has a job it is good at:

* **Python, in Monty** (pydantic's Python sandbox): the numbers. A year of synthetic daily metrics
  for four regions (hash-based noise, weekly seasonality, a trend, a few injected anomalies), then
  the analysis in pure Python: weekly totals, a centred 7-day moving average, z-score anomaly
  detection on the residual, a correlation matrix between regions and a linear-regression trend.
* **JavaScript, in pydeno** (a worker process behind an OS sandbox): the pictures. ECharts renders
  four charts (line with anomaly markers, weekly bars, correlation heatmap, regional share pie) to
  SVG strings with no DOM, and the host stitches them into one self-contained HTML page: inline
  SVG, no scripts, no external requests.

Both halves run without trusting either: neither can touch your files, network or environment. The
only things they can call are the host functions you hand them.

Run from a checkout (it uses the ECharts bundle vendored in ``vendor/libs``)::

    pip install pydeno pydantic-monty
    python examples/monty_echarts_dashboard.py          # writes dashboard.html
    python examples/monty_echarts_dashboard.py 730      # two years of data
"""

from __future__ import annotations

import html
import pathlib
import sys
import time
from typing import Any

from pydeno import WEB_POLYFILLS, IsolatedRuntime, RuntimeConfig

try:
    from pydantic_monty import Monty
except ImportError:
    sys.exit("This example pairs pydeno with Monty: pip install pydantic-monty")

LIBS = pathlib.Path(__file__).resolve().parent.parent / "vendor" / "libs"
ECHARTS_BUNDLE = "echarts-6.1.0.min.js"

SIZES = [90, 180, 365, 730]  # number of days

# --------------------------------------------------------------------------------------------
# The Python half (what the model wrote). It runs in Monty: no imports beyond `math`, no files.
# Input: DAYS (>= 60). The last expression is the result.
# --------------------------------------------------------------------------------------------

MODEL_PYTHON = """
import math

REGIONS = ["North", "South", "East", "West"]
BASE = [1200.0, 900.0, 1500.0, 700.0]
PHASE = [0.0, 1.3, 2.6, 3.9]
R = len(REGIONS)


def hash2(i, j, seed):
    h = (i * 374761393 + j * 668265263 + seed * 1274126177) % 4294967296
    h = ((h ^ (h >> 13)) * 1274126177) % 4294967296
    return (h % 100000) / 100000.0


# Anomalies injected on purpose, so the detector has something to find: [region, day, factor].
injected = []
for k in range(5):
    injected.append([k % 4, DAYS * (2 * k + 1) // 11, 2.6 if k % 2 == 0 else 0.25])

series = []
for r in range(R):
    row = []
    for d in range(DAYS):
        weekly_cycle = 1.0 + 0.25 * math.sin(2.0 * math.pi * (d % 7) / 7.0 + PHASE[r])
        trend_factor = 1.0 + 0.35 * d / DAYS
        shared = 0.08 * (hash2(d, 0, 5) - 0.5)
        noise = 0.06 * (hash2(d, r + 1, 11) + hash2(d, r + 1, 12) - 1.0)
        v = BASE[r] * weekly_cycle * trend_factor * (1.0 + shared + noise)
        for a in injected:
            if a[0] == r and a[1] == d:
                v = v * a[2]
        row.append(round(v, 2))
    series.append(row)

# Centred 7-day moving average (a full week, so the weekly cycle cancels). None at the edges.
ma = []
for r in range(R):
    row = [None] * DAYS
    for d in range(3, DAYS - 3):
        row[d] = round(sum(series[r][d - 3 : d + 4]) / 7.0, 2)
    ma.append(row)

# z-score of the relative residual ((value - moving average) / moving average), per region.
anomalies = []
for r in range(R):
    resid = []
    for d in range(3, DAYS - 3):
        resid.append((series[r][d] - ma[r][d]) / ma[r][d])
    mean = sum(resid) / len(resid)
    var = 0.0
    for x in resid:
        var += (x - mean) * (x - mean)
    sd = math.sqrt(var / len(resid))
    for idx in range(len(resid)):
        z = (resid[idx] - mean) / sd
        if abs(z) > 3.0:
            anomalies.append([r, idx + 3, series[r][idx + 3], round(z, 3)])

# Weekly totals per region.
weeks = DAYS // 7
weekly = []
for r in range(R):
    row = []
    for w in range(weeks):
        row.append(round(sum(series[r][w * 7 : w * 7 + 7]), 2))
    weekly.append(row)


def corr(a, b):
    n = len(a)
    ma_ = sum(a) / n
    mb_ = sum(b) / n
    sab = 0.0
    saa = 0.0
    sbb = 0.0
    for i in range(n):
        da = a[i] - ma_
        db = b[i] - mb_
        sab += da * db
        saa += da * da
        sbb += db * db
    return sab / math.sqrt(saa * sbb)


correlation = []
for i in range(R):
    row = []
    for j in range(R):
        row.append(round(corr(series[i], series[j]), 4))
    correlation.append(row)

# Least-squares trend of the all-region daily total.
total = []
for d in range(DAYS):
    t = 0.0
    for r in range(R):
        t += series[r][d]
    total.append(t)
xm = (DAYS - 1) / 2.0
ym = sum(total) / DAYS
sxy = 0.0
sxx = 0.0
for d in range(DAYS):
    sxy += (d - xm) * (total[d] - ym)
    sxx += (d - xm) * (d - xm)
slope = sxy / sxx
intercept = ym - slope * xm
ss_res = 0.0
ss_tot = 0.0
for d in range(DAYS):
    ss_res += (total[d] - (intercept + slope * d)) ** 2
    ss_tot += (total[d] - ym) ** 2

share = []
for r in range(R):
    share.append(round(sum(series[r]), 2))

{
    "days": DAYS,
    "regions": REGIONS,
    "series": series,
    "ma": ma,
    "injected": injected,
    "anomalies": anomalies,
    "weekly": weekly,
    "correlation": correlation,
    "share": share,
    "trend": {
        "slope": round(slope, 4),
        "intercept": round(intercept, 2),
        "r2": round(1.0 - ss_res / ss_tot, 4),
    },
}
"""

# --------------------------------------------------------------------------------------------
# The JavaScript half. It runs in pydeno with ECharts loaded; `getData()` is the host function that
# hands it the Python result, and the only way in. ECharts renders server-side: no DOM, no canvas.
# --------------------------------------------------------------------------------------------

MODEL_JAVASCRIPT = """
globalThis.buildDashboard = (width, height) => {
  const d = getData();
  const colors = ['#3b6fb6', '#e08a2c', '#3f9c6b', '#8e5bb5'];
  const render = (option) => {
    const chart = echarts.init(null, null, { renderer: 'svg', ssr: true, width, height });
    chart.setOption({ animation: false, color: colors, ...option });
    const svg = chart.renderToSVGString();
    chart.dispose();
    return svg;
  };
  const days = Array.from({ length: d.days }, (_, i) => i);

  const line = render({
    title: { text: 'Daily metric: 7-day moving average, anomalies flagged', textStyle: { fontSize: 14 } },
    legend: { top: 34 },
    grid: { top: 70, left: 60, right: 20, bottom: 40 },
    xAxis: { type: 'value', name: 'day', min: 0, max: d.days - 1 },
    yAxis: { type: 'value', scale: true },
    series: [
      ...d.regions.map((name, r) => ({
        name, type: 'line', showSymbol: false, smooth: false, connectNulls: false,
        data: days.map((day) => [day, d.ma[r][day]]),
      })),
      {
        name: 'anomaly', type: 'scatter', symbolSize: 11, z: 10,
        itemStyle: { color: '#d62728', borderColor: '#fff', borderWidth: 1 },
        data: d.anomalies.map(([r, day, v]) => [day, v]),
      },
    ],
  });

  const bar = render({
    title: { text: 'Weekly totals by region', textStyle: { fontSize: 14 } },
    legend: { top: 34 },
    grid: { top: 70, left: 70, right: 20, bottom: 40 },
    xAxis: { type: 'category', name: 'week', data: d.weekly[0].map((_, w) => w + 1) },
    yAxis: { type: 'value' },
    series: d.regions.map((name, r) => ({ name, type: 'bar', stack: 'total', data: d.weekly[r] })),
  });

  const cells = [];
  d.correlation.forEach((row, i) => row.forEach((v, j) => cells.push([j, i, v])));
  const min = Math.min(...cells.map((c) => c[2]));
  const heat = render({
    title: { text: 'Correlation between regions', textStyle: { fontSize: 14 } },
    grid: { top: 60, left: 70, right: 90, bottom: 40 },
    xAxis: { type: 'category', data: d.regions, splitArea: { show: true } },
    yAxis: { type: 'category', data: d.regions, splitArea: { show: true } },
    visualMap: {
      min: Math.floor(min * 10) / 10, max: 1, calculable: false, orient: 'vertical',
      right: 10, top: 'middle', inRange: { color: ['#f3f6fb', '#7fa6d6', '#1d4e89'] },
    },
    series: [{ type: 'heatmap', data: cells, label: { show: true, formatter: (p) => p.value[2].toFixed(2) } }],
  });

  const pie = render({
    title: { text: 'Share of total volume', textStyle: { fontSize: 14 } },
    legend: { bottom: 4 },
    series: [{
      type: 'pie', radius: ['35%', '65%'], center: ['50%', '50%'],
      label: { formatter: '{b}: {d}%' },
      data: d.regions.map((name, r) => ({ name, value: d.share[r] })),
    }],
  });

  return { svgs: { line, bar, heat, pie }, echarts: echarts.version };
};
"""

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 24px; background: #f6f7f9; color: #1c2430; }}
h1 {{ font-size: 20px; margin: 0 0 4px; }}
p.sub {{ margin: 0 0 16px; color: #566; font-size: 13px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 16px; }}
.panel {{ background: #fff; border-radius: 8px; padding: 8px; box-shadow: 0 1px 3px #0002; }}
.panel svg {{ width: 100%; height: auto; display: block; }}
</style></head><body>
<h1>{title}</h1>
<p class="sub">{subtitle}</p>
<div class="grid">
<div class="panel">{line}</div>
<div class="panel">{bar}</div>
<div class="panel">{heat}</div>
<div class="panel">{pie}</div>
</div>
</body></html>
"""


class DashboardPipeline:
    """One Monty pool and one warm pydeno runtime, reused across dashboards."""

    def __init__(self, *, jitless: bool = True) -> None:
        self.monty = Monty().__enter__()
        self.rt = IsolatedRuntime(
            RuntimeConfig(bootstrap=WEB_POLYFILLS, timeout=120.0),
            sandbox="require",
            request_timeout=300,
            jitless=jitless,
        )
        self._data: dict[str, Any] = {}
        self.rt.bind_function("getData", lambda: self._data)
        self.load_seconds = 0.0

    def load_echarts(self) -> None:
        start = time.perf_counter()
        self.rt.eval((LIBS / ECHARTS_BUNDLE).read_text() + "\n;0")
        self.rt.eval(MODEL_JAVASCRIPT + "\n;0")
        self.load_seconds = time.perf_counter() - start

    def prepare(self, days: int) -> dict[str, Any]:
        """Python half, in Monty."""
        with self.monty.checkout() as session:
            return session.feed_run(MODEL_PYTHON, inputs={"DAYS": days})

    def render(
        self, size: int, width: int = 640, height: int = 380
    ) -> tuple[str, dict[str, Any], dict[str, float]]:
        t0 = time.perf_counter()
        self._data = self.prepare(size)
        t1 = time.perf_counter()
        built = self.rt.eval(f"buildDashboard({width}, {height})")
        t2 = time.perf_counter()
        data = self._data
        trend = data["trend"]
        found = {(r, d) for r, d, _, _ in data["anomalies"]}
        injected = {(r, d) for r, d, _ in data["injected"]}
        svgs = built["svgs"]
        page = PAGE.format(
            title="Regional metrics",
            subtitle=html.escape(
                f"{data['days']} days, {len(data['regions'])} regions, "
                f"{len(data['anomalies'])} anomalies flagged, trend "
                f"{trend['slope']:+.2f}/day (R² {trend['r2']:.2f})"
            ),
            **svgs,
        )
        stats = {
            "days": data["days"],
            "regions": len(data["regions"]),
            "anomalies": len(data["anomalies"]),
            "injected": len(injected),
            "injected_found": len(injected & found),
            "slope": trend["slope"],
            "r2": trend["r2"],
            "charts": len(svgs),
            "svg_bytes": sum(len(s) for s in svgs.values()),
            "echarts": built["echarts"],
        }
        t3 = time.perf_counter()
        return (
            page,
            stats,
            {"monty_prepare": t1 - t0, "js_build": t2 - t1, "total": t3 - t0},
        )

    def close(self) -> None:
        self.rt.close()
        self.monty.__exit__(None, None, None)


PIPELINE = DashboardPipeline


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 365
    pipe = DashboardPipeline()
    try:
        print(f"sandbox: {pipe.rt.sandbox}")
        pipe.load_echarts()
        print(f"ECharts loaded into the sandbox in {pipe.load_seconds * 1000:.0f} ms")
        page, stats, t = pipe.render(days)
        pathlib.Path("dashboard.html").write_text(page, encoding="utf-8")
        print(
            f"{stats['days']} days x {stats['regions']} regions -> "
            f"{stats['anomalies']} anomalies flagged "
            f"({stats['injected_found']}/{stats['injected']} injected found), "
            f"trend {stats['slope']:+.2f}/day, R2 {stats['r2']:.3f}"
        )
        print(f"  monty analysed the data   {t['monty_prepare'] * 1000:8.0f} ms")
        print(
            f"  echarts drew {stats['charts']} charts      {t['js_build'] * 1000:8.0f} ms"
        )
        print(
            f"wrote dashboard.html ({len(page) / 1024:.0f} KiB) in {t['total'] * 1000:.0f} ms total"
        )
    finally:
        pipe.close()


if __name__ == "__main__":
    main()
