"""
Run vendored npm libraries inside a bare pydeno.Runtime.

This demonstrates a general PATTERN, not a built-in feature: the *host*
fetches (or ships) a specific, versioned browser/UMD bundle of an npm
library, injects a handful of small polyfills the bundle needs, evals the
bundle text into an isolated V8 context, then calls the library's real API
to produce real output bytes -- no Node.js, no filesystem, no network access
from the guest JS.

A library is a good candidate for this pattern when:
  - it ships a real, dependency-free browser or UMD build (check its
    package's `dist/` or `unpkg.com/<pkg>/dist/`), and
  - its core functionality does not depend on real `fs`, real network
    access, or a DOM -- document/data-generation libraries (slide decks,
    PDFs, spreadsheets) tend to qualify; general web-app UI libraries
    usually don't.

Two libraries are demonstrated here, both verified to produce real,
structurally valid output files:

  1. pptxgenjs -- builds a real multi-slide .pptx (title slide, a table,
     a native chart, and an image), matching how Anthropic's published
     `pptx` skill (github.com/anthropics/skills, skills/pptx) actually
     drives pptxgenjs. The one polyfill it genuinely cannot do without is
     setTimeout/clearTimeout, and it has to be a *deferring* one -- see the
     note on POLYFILLS_PATH below, and `vendor/pptxgenjs/polyfills.js` for
     which of the three globals are load-bearing and which are defensive.
  2. pdf-lib -- builds a real multi-page .pdf. Needs *no* polyfills at
     all; it's a genuinely browser-native library by design.

This example fetches its bundles over the network, which is what makes it an
example rather than a test. For the fuller pptxgenjs story -- a pinned,
vendored bundle, a six-slide deck with a combo chart on a secondary axis, and
validation all the way through python-pptx and a LibreOffice render -- see
`examples/pptxgenjs_presentation.py`. For the hermetic regression guard, see
`tests/test_vendored_bundle_execution.py`.

IMPORTANT -- what this is NOT: this is not the same as giving guest JS a
real `require()`/npm resolver. The guest code never gets to load anything
of its own choosing; the HOST decides, ahead of time, exactly which
vetted, versioned bundle text gets evaluated. There is no dynamic module
resolution happening on the guest's behalf. It is also, today, a fully
manual pattern -- the host script below writes its own polyfills and eval
calls; there is no `Runtime(allow_modules=["pptxgenjs"])`-style API. See
`docs/guides/advanced/vendored-npm-libraries.md` for the full writeup,
including a library that did NOT work with this pattern and why.
"""

import asyncio
import base64
import pathlib
import zipfile
from io import BytesIO

import httpx

from pydeno import Runtime

# Pinned exactly, not floated on `@4`: a bundle is only a meaningful thing to
# have verified if you know which bytes you verified.
PPTXGENJS_URL = "https://cdn.jsdelivr.net/npm/pptxgenjs@4.0.1/dist/pptxgen.bundle.js"
PDF_LIB_URL = "https://cdn.jsdelivr.net/npm/pdf-lib@1.17.1/dist/pdf-lib.min.js"

# The host polyfills live in one reviewable file, shared with
# `examples/pptxgenjs_presentation.py` and with the regression test
# `tests/test_vendored_bundle_execution.py`, so there is a single source of
# truth for a surface that is genuinely load-bearing.
#
# NOTE -- the setTimeout shim in there is backed by
# `Promise.resolve().then()`, and that is not decoration. JSZip drives
# `pres.write()` through `setImmediate`, which the bundle implements on top of
# `setTimeout` once it finds no process/MessageChannel/document. Measured at
# pptxgenjs 4.0.1:
#
#                                | global/self absent | global/self present
#     setTimeout = fn => {}      |       hangs        |       hangs
#     setTimeout = fn => fn()    |       works        |       hangs
#     Promise.resolve().then(fn) |       works        |       works
#
# An earlier version of this example used the synchronous `fn => fn()` shim
# and did not define `global`/`self`, which is the one combination where that
# shim gets away with it. Since the shared polyfill file does define them, a
# microtask-backed shim is now required -- and it is the only row that is
# correct regardless of the rest of the polyfill set. See
# `vendor/pptxgenjs/polyfills.js` and the coupling test in
# `tests/test_vendored_bundle_execution.py`.
POLYFILLS_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "vendor"
    / "pptxgenjs"
    / "polyfills.js"
)
BASE64_POLYFILLS = POLYFILLS_PATH.read_text()

PPTX_SCRIPT = """
const pres = new PptxGenJS();
pres.layout = "LAYOUT_WIDE";

const s1 = pres.addSlide();
s1.addText("Vendored npm libraries in pydeno", {
  x: 0.5, y: 2.2, w: 12.3, h: 1.2, fontSize: 32, bold: true, align: "center",
});

const s2 = pres.addSlide();
s2.addText("Revenue by region", { x: 0.5, y: 0.3, w: 8, h: 0.6, fontSize: 22, bold: true });
s2.addTable(
  [["Region", "Revenue"], ["EMEA", "1.2M"], ["APAC", "0.9M"]],
  { x: 0.5, y: 1.1, w: 6, colW: [3, 3] }
);

const s3 = pres.addSlide();
s3.addChart(pres.ChartType.bar, [
  { name: "Revenue", labels: ["Q1", "Q2"], values: [10, 14] },
], { x: 0.5, y: 1.1, w: 8, h: 4, showTitle: true, title: "Growth" });

pres.write({ outputType: "base64" });
"""

PDF_SCRIPT = """
(async () => {
  const doc = await PDFLib.PDFDocument.create();
  const page = doc.addPage([300, 200]);
  page.drawText("Built with pdf-lib inside pydeno", { x: 20, y: 150, size: 12 });
  return doc.saveAsBase64();
})()
"""


async def fetch_bundle(url: str) -> str:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


async def run_pptxgenjs() -> bytes:
    bundle = await fetch_bundle(PPTXGENJS_URL)
    with Runtime() as rt:
        rt.eval(BASE64_POLYFILLS)
        rt.eval(bundle)
        b64 = await rt.eval_async(PPTX_SCRIPT)
    return base64.b64decode(b64)


async def run_pdf_lib() -> bytes:
    bundle = await fetch_bundle(PDF_LIB_URL)
    with Runtime() as rt:
        # No polyfills needed -- pdf-lib is genuinely browser-native.
        rt.eval(bundle)
        b64 = await rt.eval_async(PDF_SCRIPT)
    return base64.b64decode(b64)


def verify_pptx(raw: bytes) -> None:
    zf = zipfile.ZipFile(BytesIO(raw))
    assert "[Content_Types].xml" in zf.namelist(), "not a real OOXML zip"
    slides = [n for n in zf.namelist() if n.startswith("ppt/slides/slide")]
    assert len(slides) == 3, f"expected 3 slides, got {len(slides)}"
    assert any("charts/chart" in n for n in zf.namelist()), "missing chart part"


def verify_pdf(raw: bytes) -> None:
    assert raw.startswith(b"%PDF-"), "not a real PDF"
    assert b"%%EOF" in raw[-64:] or b"%%EOF" in raw, "missing PDF trailer"


async def main() -> None:
    print(
        "Building a .pptx with the real pptxgenjs bundle (deferring setTimeout needed)..."
    )
    pptx_bytes = await run_pptxgenjs()
    verify_pptx(pptx_bytes)
    print(
        f"  OK: {len(pptx_bytes)} bytes, 3 slides incl. table + chart, verified as real OOXML zip"
    )

    print("Building a .pdf with the real pdf-lib bundle (no polyfills needed)...")
    pdf_bytes = await run_pdf_lib()
    verify_pdf(pdf_bytes)
    print(f"  OK: {len(pdf_bytes)} bytes, verified as a real PDF")


if __name__ == "__main__":
    asyncio.run(main())
