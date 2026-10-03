"""Spreadsheet to board deck: Monty analyses the data, pptxgenjs builds the .pptx, neither is trusted.

An agent asked to "turn this expense spreadsheet into a board deck" writes two programs, because
each language has a job it is good at (this combines pydantic/monty's ``spreadsheet`` and
``expense_analysis`` examples with pydeno's JavaScript sandbox):

* **Python, in Monty**: reads the sheet through a read-only host wrapper and computes the findings:
  totals, month-over-month growth, top and bottom items, outliers and a short narrative.
* **JavaScript, in pydeno** (a worker process behind an OS sandbox): the vendored, unmodified
  pptxgenjs bundle turns the findings into a real ``.pptx``: title, findings, two native charts and
  a table of the top items.

Both sandboxes draw on ONE host tool budget, and neither can reach your files, network or
environment: the only things they can call are the host functions you hand them. The workbook is
generated in plain Python (``openpyxl`` is not a dependency) and exposed read-only, in pages.

Run from a checkout::

    pip install pydeno pydantic-monty
    python examples/monty_spreadsheet_deck.py            # writes spreadsheet_deck.pptx
    python examples/monty_spreadsheet_deck.py 2000       # a bigger spreadsheet
"""

from __future__ import annotations

import asyncio
import base64
import pathlib
import sys
import time
from typing import Any

from pydeno import IsolatedRuntime, RuntimeConfig

try:
    from pydantic_monty import Monty
except ImportError:
    sys.exit("This example pairs pydeno with Monty: pip install pydeno pydantic-monty")

VENDOR = pathlib.Path(__file__).resolve().parent.parent / "vendor" / "pptxgenjs"
SIZES = [200, 1000, 5000]
PAGE = 100  # rows per `sheet_rows` call

# --------------------------------------------------------------------------------------------
# The "spreadsheet": plain rows, deterministic, with one planted outlier.
# --------------------------------------------------------------------------------------------

ITEMS = [
    ("Cloud hosting", "Infrastructure", 4200.0),
    ("Salaries", "People", 9800.0),
    ("Office rent", "Facilities", 3100.0),
    ("Marketing", "Growth", 2600.0),
    ("Travel", "Operations", 1500.0),
    ("Software licences", "Infrastructure", 1900.0),
    ("Legal", "Operations", 1100.0),
    ("Training", "People", 700.0),
]


def make_rows(count: int) -> list[dict[str, Any]]:
    """`count` expense rows over 12 months. A tiny LCG keeps it reproducible without `random`."""
    state = 12345
    rows: list[dict[str, Any]] = []
    for k in range(count):
        state = (state * 1103515245 + 12345) % 2147483648
        item, category, base = ITEMS[state % len(ITEMS)]
        state = (state * 1103515245 + 12345) % 2147483648
        month = k * 12 // count + 1  # rows are chronological
        trend = 1.0 + 0.03 * month  # costs creep up through the year
        noise = 0.6 + (state % 1000) / 1250.0  # 0.6 .. 1.4
        rows.append(
            {
                "month": f"2025-{month:02d}",
                "item": item,
                "category": category,
                "amount": round(base * trend * noise / 10.0, 2),
            }
        )
    if rows:
        rows[count // 2]["amount"] = (
            25000.0  # the planted outlier a good analyst must flag
        )
    return rows


class Sheet:
    """A read-only view of the workbook. The model gets functions, never the list."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = tuple(tuple(r.items()) for r in rows)

    def info(self) -> dict[str, Any]:
        return {
            "columns": ["month", "item", "category", "amount"],
            "rows": len(self._rows),
        }

    def rows(self, start: int, stop: int) -> list[dict[str, Any]]:
        start = max(0, int(start))
        stop = min(len(self._rows), int(stop), start + PAGE)
        return [dict(r) for r in self._rows[start:stop]]


class Budget:
    """Total tool calls the model may make this turn, whichever language it calls from."""

    def __init__(self, calls: int) -> None:
        self.total, self.left = calls, calls

    @property
    def used(self) -> int:
        return self.total - self.left

    def tool(self, fn: Any) -> Any:
        def guarded(*args: Any) -> Any:
            if self.left <= 0:
                raise RuntimeError("tool budget exhausted")
            self.left -= 1
            return fn(*args)

        return guarded


# --------------------------------------------------------------------------------------------
# The Python half (what the model wrote). It runs in Monty: no files, only the host functions.
# --------------------------------------------------------------------------------------------

MODEL_PYTHON = """
import math

info = sheet_info()
total_rows = info["rows"]

by_month = {}
by_item = {}
amounts = []
rows = []
start = 0
while start < total_rows:
    page = sheet_rows(start, start + 100)
    if len(page) == 0:
        break
    for row in page:
        by_month[row["month"]] = by_month.get(row["month"], 0.0) + row["amount"]
        by_item[row["item"]] = by_item.get(row["item"], 0.0) + row["amount"]
        amounts.append(row["amount"])
        rows.append(row)
    start += len(page)

grand_total = sum(amounts)
mean = grand_total / len(amounts)
variance = sum([(a - mean) * (a - mean) for a in amounts]) / len(amounts)
std = math.sqrt(variance)

months = sorted(by_month.keys())
monthly = [{"month": m, "total": round(by_month[m], 2)} for m in months]
growth = []
for i in range(1, len(months)):
    prev = by_month[months[i - 1]]
    growth.append({"month": months[i], "pct": round((by_month[months[i]] - prev) / prev * 100.0, 1)})

ranked = sorted(by_item.keys(), key=lambda k: (-by_item[k], k))
items = [{"item": k, "total": round(by_item[k], 2), "share": round(by_item[k] / grand_total * 100.0, 1)} for k in ranked]

outliers = []
for row in rows:
    if row["amount"] > mean + 4.0 * std:
        outliers.append({"month": row["month"], "item": row["item"], "amount": row["amount"]})

best = max(growth, key=lambda g: g["pct"])
worst = min(growth, key=lambda g: g["pct"])
top = items[0]
bottom = items[-1]
narrative = [
    f"Total spend across {total_rows} rows was {grand_total:,.0f}.",
    f"{top['item']} is the largest cost line at {top['total']:,.0f} ({top['share']}% of spend).",
    f"{bottom['item']} is the smallest at {bottom['total']:,.0f}.",
    f"Spend grew fastest in {best['month']} ({best['pct']:+.1f}% month over month) and slowest in {worst['month']} ({worst['pct']:+.1f}%).",
]
if len(outliers) > 0:
    o = outliers[0]
    narrative.append(f"{len(outliers)} outlier(s) need review, e.g. {o['item']} in {o['month']} at {o['amount']:,.0f}.")
else:
    narrative.append("No outliers were found.")

{
    "rows": total_rows,
    "total": round(grand_total, 2),
    "monthly": monthly,
    "growth": growth,
    "items": items,
    "top": items[:5],
    "bottom": items[-3:],
    "outliers": outliers,
    "narrative": narrative,
}
"""

# --------------------------------------------------------------------------------------------
# The JavaScript half. `getFindings()` and `sheetInfo()` are budgeted host functions.
# --------------------------------------------------------------------------------------------

MODEL_JAVASCRIPT = """
(async () => {
  const f = getFindings();
  const info = sheetInfo();
  const NAVY = "1F2A44", INK = "363636", GRID = "E6E9F0";
  const title = (s, text) => s.addText(text, {
    x: 0.5, y: 0.35, w: 12.3, h: 0.7, fontSize: 26, bold: true, color: NAVY });
  const money = (n) => n.toLocaleString("en-US", { maximumFractionDigits: 0 });

  const pres = new PptxGenJS();
  pres.layout = "LAYOUT_WIDE";
  pres.title = "Spend review";

  const s1 = pres.addSlide();
  s1.background = { color: NAVY };
  s1.addText("Spend review", { x: 0.6, y: 2.2, w: 12.1, h: 1.0, fontSize: 40, bold: true,
    color: "FFFFFF", align: "center" });
  s1.addText(`${info.rows} line items, total ${money(f.total)}`, { x: 0.6, y: 3.3, w: 12.1,
    h: 0.6, fontSize: 18, color: "AAB6D3", align: "center" });

  const s2 = pres.addSlide();
  title(s2, "Findings");
  s2.addText(f.narrative.map((t) => ({ text: t, options: { bullet: true, breakLine: true } })), {
    x: 0.6, y: 1.3, w: 12.1, h: 5.0, fontSize: 18, color: INK, valign: "top", paraSpaceAfter: 10 });

  const s3 = pres.addSlide();
  title(s3, "Monthly spend");
  const peak = Math.max(...f.monthly.map((m) => m.total));
  s3.addChart(pres.ChartType.line,
    [{ name: "Spend", labels: f.monthly.map((m) => m.month), values: f.monthly.map((m) => m.total) }],
    { x: 0.5, y: 1.2, w: 12.3, h: 5.2, showTitle: true, title: "Spend per month",
      titleColor: NAVY, titleFontSize: 16, showLegend: true, legendPos: "b", legendColor: INK,
      chartColors: ["4472C4"], lineDataSymbol: "circle", lineSize: 2,
      catAxisLabelColor: INK, valAxisLabelColor: INK,
      valAxisMinVal: 0, valAxisMaxVal: Math.ceil(peak * 1.1),
      valGridLine: { style: "solid", size: 1, color: GRID } });

  const s4 = pres.addSlide();
  title(s4, "Where the money goes");
  const biggest = Math.max(...f.top.map((t) => t.total));
  s4.addChart(pres.ChartType.bar,
    [{ name: "Total", labels: f.top.map((t) => t.item), values: f.top.map((t) => t.total) }],
    { x: 0.5, y: 1.2, w: 12.3, h: 5.2, showTitle: true, title: "Top items by total",
      titleColor: NAVY, titleFontSize: 16, showLegend: true, legendPos: "b", legendColor: INK,
      chartColors: ["ED7D31"], catAxisLabelColor: INK, valAxisLabelColor: INK,
      valAxisMinVal: 0, valAxisMaxVal: Math.ceil(biggest * 1.1),
      valGridLine: { style: "solid", size: 1, color: GRID } });

  const s5 = pres.addSlide();
  title(s5, "Top items");
  const head = (t) => ({ text: t, options: { bold: true, color: "FFFFFF", fill: { color: NAVY } } });
  s5.addTable(
    [[head("Item"), head("Total"), head("Share")],
     ...f.top.map((t) => [t.item, money(t.total), `${t.share}%`])],
    { x: 0.5, y: 1.3, w: 9.0, colW: [4.5, 2.5, 2.0], border: { pt: 1, color: "C8D0E0" }, fontSize: 16 });

  return await pres.write({ outputType: "base64" });
})()
"""


class DeckPipeline:
    """One Monty pool and one warm pydeno runtime, reused across decks."""

    def __init__(self, *, jitless: bool = True, budget_calls: int = 1000) -> None:
        self.monty = Monty().__enter__()
        self.budget = Budget(budget_calls)
        self.rt = IsolatedRuntime(
            RuntimeConfig(timeout=120.0),
            sandbox="require",
            request_timeout=300,
            jitless=jitless,
        )
        self._sheet = Sheet([])
        self._findings: dict[str, Any] = {}
        # What the Python half can call, and what the JavaScript half can call: one budget.
        self.python_tools = {
            "sheet_info": self.budget.tool(lambda: self._sheet.info()),
            "sheet_rows": self.budget.tool(lambda a, b: self._sheet.rows(a, b)),
        }
        self.rt.bind_function("getFindings", self.budget.tool(lambda: self._findings))
        self.rt.bind_function("sheetInfo", self.budget.tool(lambda: self._sheet.info()))
        self.rt.eval((VENDOR / "polyfills.js").read_text() + "\n;0")
        self.rt.eval((VENDOR / "pptxgen.bundle.js").read_text() + "\n;0")

    def prepare(self, rows: int | list[dict[str, Any]]) -> dict[str, Any]:
        """Python half, in Monty. `rows` is a row count or the rows themselves."""
        data = make_rows(rows) if isinstance(rows, int) else rows
        self._sheet = Sheet(data)
        with self.monty.checkout() as session:
            self._findings = session.feed_run(
                MODEL_PYTHON, external_lookup=self.python_tools
            )
        return self._findings

    def render(self, size: int) -> tuple[bytes, dict[str, Any], dict[str, float]]:
        t0 = time.perf_counter()
        findings = self.prepare(size)
        t1 = time.perf_counter()
        b64 = asyncio.run(self.rt.eval_async(MODEL_JAVASCRIPT, timeout=300))
        t2 = time.perf_counter()
        stats = {
            "rows": findings["rows"],
            "total": findings["total"],
            "slides": 5,
            "charts": 2,
            "outliers": len(findings["outliers"]),
            "tool_calls": self.budget.used,
            "findings": findings,
        }
        return (
            base64.b64decode(b64),
            stats,
            {"monty_prepare": t1 - t0, "js_build": t2 - t1, "total": t2 - t0},
        )

    def close(self) -> None:
        self.rt.close()
        self.monty.__exit__(None, None, None)


PIPELINE = DeckPipeline


def main() -> None:
    size = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    pipe = DeckPipeline()
    try:
        print(f"sandbox: {pipe.rt.sandbox}")
        pptx, stats, t = pipe.render(size)
        pathlib.Path("spreadsheet_deck.pptx").write_bytes(pptx)
        print(
            f"{size} rows, total {stats['total']:,.0f}, {stats['outliers']} outlier(s):"
        )
        for sentence in stats["findings"]["narrative"]:
            print(f"  - {sentence}")
        print(f"  monty analysed the sheet  {t['monty_prepare'] * 1000:8.0f} ms")
        print(f"  pptxgenjs built the deck  {t['js_build'] * 1000:8.0f} ms")
        print(
            f"wrote spreadsheet_deck.pptx ({len(pptx) / 1024:.0f} KiB) in {t['total'] * 1000:.0f} ms; "
            f"tool calls used: {stats['tool_calls']} of {pipe.budget.total}"
        )
    finally:
        pipe.close()


if __name__ == "__main__":
    main()
