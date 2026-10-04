"""A Julia set as a 3D relief: Monty counts, three.js builds the landscape.

This is Monty's own `julia` example (an ASCII Julia set) taken one step further. The Python half
runs in Monty and does what it is good at, a million tiny floating-point iterations: for every
point of a grid it counts how many steps `z -> z*z + c` takes to escape. The JavaScript half runs in
pydeno with three.js and turns those counts into a mountain range whose ridges follow the fractal's
boundary, paints it by escape time, and exports a binary glTF (``.glb``).

Neither half is trusted: neither can touch your files, network or environment.

    pip install pydeno pydantic-monty
    python examples/monty_three_julia.py            # writes julia.glb
    python examples/monty_three_julia.py 193        # a finer grid
"""

from __future__ import annotations

import asyncio
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
THREE_BUNDLE = "three-0.180.0-gltf.bundle.js"

# --------------------------------------------------------------------------------------------
# The Python half. No imports and no complex numbers: plain floats, so it runs in Monty and in CPython
# with identical results (the tests check that).
# --------------------------------------------------------------------------------------------

MODEL_PYTHON = """
counts = []
inside = 0
for row in range(N):
    im = 1.5 - 3.0 * row / (N - 1)
    for col in range(N):
        x = -1.5 + 3.0 * col / (N - 1)
        y = im
        i = 0
        while i < MAX_ITER and x * x + y * y <= 4.0:
            x, y = x * x - y * y + CR, 2.0 * x * y + CI
            i += 1
        counts.append(i)
        if i == MAX_ITER:
            inside += 1

{"n": N, "counts": counts, "inside": inside, "max_iter": MAX_ITER}
"""

# --------------------------------------------------------------------------------------------
# The JavaScript half: relief mesh, colours by escape time, glTF export.
# --------------------------------------------------------------------------------------------

MODEL_JAVASCRIPT = """
globalThis.buildScene = () => {
  const { n, counts, max_iter, inside } = getJulia();
  const size = 100, scale = 18;

  const geo = new THREE.PlaneGeometry(size, size, n - 1, n - 1);
  geo.rotateX(-Math.PI / 2);
  const pos = geo.attributes.position;
  const colors = new Float32Array(pos.count * 3);
  const color = new THREE.Color();
  // Heights from escape time: the flat basin where points never escape, steep walls along the fractal's
  // boundary. Neighbouring points near the boundary differ a lot, so smooth the *heights* (two passes of a
  // 3x3 average); the colours and the counts themselves are left exactly as Monty computed them.
  let heights = new Float32Array(n * n);
  for (let k = 0; k < n * n; k++) {
    heights[k] = counts[k] === max_iter ? 0 : scale * Math.sqrt(counts[k] / max_iter);
  }
  for (let pass = 0; pass < 2; pass++) {
    const next = new Float32Array(n * n);
    for (let j = 0; j < n; j++) {
      for (let i = 0; i < n; i++) {
        let sum = 0, cnt = 0;
        for (let dj = -1; dj <= 1; dj++) {
          for (let di = -1; di <= 1; di++) {
            const jj = j + dj, ii = i + di;
            if (jj >= 0 && jj < n && ii >= 0 && ii < n) { sum += heights[jj * n + ii]; cnt++; }
          }
        }
        next[j * n + i] = sum / cnt;
      }
    }
    heights = next;
  }
  let peak = 0;
  for (let k = 0; k < pos.count; k++) {
    const t = counts[k] / max_iter;               // 0 = escaped at once, 1 = never escapes
    pos.setY(k, heights[k]);
    peak = Math.max(peak, heights[k]);
    if (counts[k] === max_iter) color.setRGB(0.05, 0.05, 0.12);
    else color.setHSL(0.62 - 0.62 * Math.sqrt(t), 0.85, 0.35 + 0.35 * t);
    colors.set([color.r, color.g, color.b], k * 3);
  }
  geo.computeVertexNormals();
  geo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
  const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ vertexColors: true }));

  const scene = new THREE.Scene();
  scene.add(mesh);
  scene.updateMatrixWorld(true);
  globalThis.__scene = scene;

  let area = 0;
  const idx = geo.index, p = geo.attributes.position, tri = new THREE.Triangle();
  const a = new THREE.Vector3(), b = new THREE.Vector3(), c = new THREE.Vector3();
  for (let t = 0; t < idx.count; t += 3) {
    tri.set(a.fromBufferAttribute(p, idx.getX(t)), b.fromBufferAttribute(p, idx.getX(t + 1)),
            c.fromBufferAttribute(p, idx.getX(t + 2)));
    area += tri.getArea();
  }
  return {
    triangles: idx.count / 3, peak, surfaceArea: Math.round(area),
    insideFraction: inside / (n * n),
  };
};

globalThis.exportGlb = () => new Promise((resolve, reject) =>
  new GLTFExporter().parse(globalThis.__scene, resolve, reject, { binary: true }));
"""

# A classic Julia set with a rich, connected boundary.
C = (-0.7, 0.27015)
MAX_ITER = 80


class JuliaPipeline:
    """One Monty pool and one warm pydeno runtime, reused across renders."""

    def __init__(self, *, jitless: bool = True) -> None:
        self.monty = Monty().__enter__()
        self.rt = IsolatedRuntime(
            RuntimeConfig(bootstrap=WEB_POLYFILLS, timeout=120.0),
            sandbox="require",
            request_timeout=300,
            jitless=jitless,
        )
        self._julia: dict[str, Any] = {}
        self.rt.bind_function("getJulia", lambda: self._julia)
        self.load_seconds = 0.0

    def load_three(self) -> None:
        start = time.perf_counter()
        self.rt.eval((LIBS / THREE_BUNDLE).read_text() + "\n;0")
        self.rt.eval(MODEL_JAVASCRIPT + "\n;0")
        self.load_seconds = time.perf_counter() - start

    def prepare(self, n: int) -> dict[str, Any]:
        """Python half, in Monty."""
        with self.monty.checkout() as session:
            return session.feed_run(
                MODEL_PYTHON,
                inputs={"N": n, "CR": C[0], "CI": C[1], "MAX_ITER": MAX_ITER},
            )

    def render(self, n: int) -> tuple[bytes, dict[str, Any], dict[str, float]]:
        t0 = time.perf_counter()
        self._julia = self.prepare(n)
        t1 = time.perf_counter()
        stats = self.rt.eval("buildScene()")
        t2 = time.perf_counter()
        glb = asyncio.run(self.rt.eval_async("exportGlb()", timeout=300))
        t3 = time.perf_counter()
        return (
            bytes(glb),
            stats,
            {
                "monty_prepare": t1 - t0,
                "three_build": t2 - t1,
                "glb_export": t3 - t2,
                "total": t3 - t0,
            },
        )

    def close(self) -> None:
        self.rt.close()
        self.monty.__exit__(None, None, None)


SIZES = [65, 129, 193]
PIPELINE = JuliaPipeline


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 129
    pipe = JuliaPipeline()
    try:
        print(f"sandbox: {pipe.rt.sandbox}")
        pipe.load_three()
        glb, stats, t = pipe.render(n)
        pathlib.Path("julia.glb").write_bytes(glb)
        print(
            f"{n}x{n} grid, c = {C[0]} + {C[1]}i -> {stats['triangles']} triangles, "
            f"{stats['insideFraction']:.1%} of the plane never escapes, peak height {stats['peak']:.1f}"
        )
        print(f"  monty counted escapes     {t['monty_prepare'] * 1000:8.0f} ms")
        print(f"  three.js built the relief {t['three_build'] * 1000:8.0f} ms")
        print(f"  glTF export               {t['glb_export'] * 1000:8.0f} ms")
        print(
            f"wrote julia.glb ({len(glb) / 1024:.0f} KiB) in {t['total'] * 1000:.0f} ms total"
        )
    finally:
        pipe.close()


if __name__ == "__main__":
    main()
