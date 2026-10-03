"""Python and JavaScript, both sandboxed, in one agent turn.

A model that writes code usually wants the best language for each half of a job: Python for
wrangling data, JavaScript for what only the JS ecosystem does well (here: a Vega-Lite chart). This
example runs both halves without trusting either:

* the model's **Python** runs in Monty (pydantic's Python sandbox: ``pip install pydantic-monty``),
* the model's **JavaScript** runs in pydeno's ``IsolatedRuntime`` (worker process + OS sandbox),
* both call the **same host tool** through **one shared call budget**, and neither can reach your
  files, network or environment.

Run from a checkout (it uses the Vega bundles vendored in ``vendor/libs``)::

    pip install pydeno pydantic-monty
    python examples/monty_and_pydeno.py        # writes sales.svg
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

from pydeno import WEB_POLYFILLS, IsolatedRuntime, JavaScriptError, RuntimeConfig

try:
    from pydantic_monty import Monty, MontyRuntimeError
except ImportError:
    sys.exit("This example pairs pydeno with Monty: pip install pydantic-monty")

LIBS = pathlib.Path(__file__).resolve().parent.parent / "vendor" / "libs"

# --------------------------------------------------------------------------------------------
# Host side: one tool, one budget, shared by both sandboxes
# --------------------------------------------------------------------------------------------

SALES = [
    {"region": "North", "units": 120, "price": 9.5},
    {"region": "South", "units": 80, "price": 12.0},
    {"region": "East", "units": 200, "price": 7.25},
    {"region": "West", "units": 150, "price": 8.0},
    {"region": "North", "units": 60, "price": 10.0},
    {"region": "East", "units": 40, "price": 7.5},
]


class Budget:
    """Total tool calls the model may make this turn, whichever language it calls from."""

    def __init__(self, calls: int) -> None:
        self.total, self.left = calls, calls

    def tool(self, fn):
        def guarded(*args):
            if self.left <= 0:
                raise RuntimeError("tool budget exhausted")
            self.left -= 1
            return fn(*args)

        return guarded


budget = Budget(calls=5)
fetch_sales = budget.tool(lambda: SALES)

# --------------------------------------------------------------------------------------------
# What the model wrote (hard-coded here; in real use it comes back from your LLM)
# --------------------------------------------------------------------------------------------

MODEL_PYTHON = """
totals = {}
for row in fetch_sales():                      # a host tool, called from the Python sandbox
    amount = row["units"] * row["price"]
    if row["region"] in totals:
        totals[row["region"]] += amount
    else:
        totals[row["region"]] = amount
best = None
for region in totals:
    if best is None or totals[region] > totals[best]:
        best = region
print(f"best region: {best} ({totals[best]:.0f})")
{"totals": totals, "best": best}               # the value of the last expression comes back
"""

MODEL_JAVASCRIPT = """
(async () => {
  const { totals, best } = getAnalysis();      // the Python result, handed over by the host
  const units = {};                            // the same tool, now called from JavaScript
  for (const r of fetch_sales()) units[r.region] = (units[r.region] || 0) + r.units;

  const spec = {
    $schema: 'https://vega.github.io/schema/vega-lite/v5.json',
    title: `Revenue by region (best: ${best})`,
    width: 320, height: 200,
    data: { values: Object.entries(totals).map(([region, revenue]) =>
      ({ region, revenue, units: units[region], best: region === best })) },
    mark: { type: 'bar', cornerRadiusEnd: 4 },
    encoding: {
      x: { field: 'region', type: 'nominal', axis: { labelAngle: 0 } },
      y: { field: 'revenue', type: 'quantitative' },
      color: { condition: { test: 'datum.best', value: '#2b8a3e' }, value: '#adb5bd' },
    },
  };
  const view = new vega.View(vega.parse(vegaLite.compile(spec).spec), { renderer: 'none' });
  return await view.toSVG();
})()
"""

# The same attempt in each language: reach outside. Neither sandbox lets it through.
NASTY_PYTHON = "open('/etc/passwd').read()"
NASTY_JAVASCRIPT = "fetch('https://example.com')"


def run_python(pool: Monty) -> dict:
    with pool.checkout() as session:
        analysis = session.feed_run(
            MODEL_PYTHON,
            external_lookup={"fetch_sales": fetch_sales},
            print_callback=lambda stream, text: print(f"  [python] {text}", end=""),
        )
        try:
            session.feed_run(NASTY_PYTHON)
        except MontyRuntimeError as exc:
            print(f"  [python] refused: {str(exc).splitlines()[-1][:70]}")
    return analysis


async def run_javascript(analysis: dict) -> str:
    libs = "\n;\n".join(
        (LIBS / name).read_text()
        for name in ("vega-6.4.0.min.js", "vega-lite-6.4.3.min.js")
    )
    with IsolatedRuntime(
        RuntimeConfig(bootstrap=WEB_POLYFILLS), sandbox="require"
    ) as rt:
        rt.bind_function("getAnalysis", lambda: analysis)
        rt.bind_function("fetch_sales", fetch_sales)
        await rt.eval_async(libs + "\n;0", timeout=30)
        try:
            rt.eval(NASTY_JAVASCRIPT)
        except JavaScriptError as exc:
            print(f"  [js]     refused: {exc}"[:90])
        svg = await rt.eval_async(MODEL_JAVASCRIPT, timeout=30)
        print(f"  [js]     sandbox: {rt.sandbox}")
    return svg


def main() -> None:
    print("1. Python half, in Monty")
    with Monty() as pool:
        analysis = run_python(pool)
    print("2. JavaScript half, in pydeno")
    svg = asyncio.run(run_javascript(analysis))
    pathlib.Path("sales.svg").write_text(svg)
    print(
        f"3. wrote sales.svg ({len(svg)} bytes); tool calls used: {budget.total - budget.left} of {budget.total}"
    )


if __name__ == "__main__":
    main()
