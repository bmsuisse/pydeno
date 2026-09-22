"""ToolBridge: give a sandbox several callable tools, safely.

Shows the three things ToolBridge adds over raw `bind_function`:
  1. a total call budget across all tools,
  2. typed errors the model's JS can branch on,
  3. console output routed back to Python.

Run with:  python examples/tool_bridge.py
"""

from __future__ import annotations

import asyncio

from pydeno import Runtime, RuntimeConfig, ToolBridge, ToolNotFoundError

# --------------------------------------------------------------------------
# The tools. Sync and async both work; ToolBridge detects which is which.
# --------------------------------------------------------------------------

_WEATHER = {"Zurich": "72F and sunny", "Oslo": "48F and raining"}


def get_weather(city: str) -> str:
    """A tool that can fail in two distinguishable ways."""
    if not city:
        raise ValueError("city must be a non-empty string")
    if city not in _WEATHER:
        raise ToolNotFoundError(f"no weather station for {city}")
    return _WEATHER[city]


async def send_email(to: str, subject: str, body: str) -> bool:
    """An async tool becomes an awaitable JS function."""
    await asyncio.sleep(0)
    print(f"  [python] would send to={to!r} subject={subject!r} ({len(body)} bytes)")
    return True


async def main() -> None:
    # Console output from the sandbox comes back here instead of vanishing.
    console: list[tuple[str, list]] = []

    bridge = ToolBridge(
        {"get_weather": get_weather, "send_email": send_email},
        # Total across ALL tools, not per tool. Five is exactly what the
        # steps below spend, so step 3 lands on the refusal.
        max_calls=5,
    )

    config = RuntimeConfig(
        on_console=lambda level, args: console.append((level, args)),
        timeout=5.0,
    )

    with Runtime(config) as runtime:
        bridge.attach(runtime)

        # ------------------------------------------------------------------
        # 1. The happy path: model-written JS calling real Python tools.
        # ------------------------------------------------------------------
        report = await runtime.eval_async("""
          (async () => {
            const cities = ['Zurich', 'Oslo'];
            const lines = [];
            for (const city of cities) {
              const weather = tools.get_weather(city);
              console.log('fetched', {city, weather});
              lines.push(`${city}: ${weather}`);
            }
            await tools.send_email('ops@example.com', 'Weather', lines.join('\\n'));
            return lines.length;
          })()
        """)
        print(f"1. reported on {report} cities")

        # ------------------------------------------------------------------
        # 2. Typed errors: the JS branches on the Python exception class.
        # ------------------------------------------------------------------
        classified = runtime.eval("""
          const probe = (city) => {
            try { tools.get_weather(city); return 'ok'; }
            catch (e) {
              if (e.name === 'ToolNotFoundError') return 'no-such-station';
              if (e.name === 'ValueError')        return 'bad-input';
              return 'unexpected:' + e.name;
            }
          };
          [probe('Atlantis'), probe('')].join(', ')
        """)
        print(f"2. typed errors -> {classified}")

        # ------------------------------------------------------------------
        # 3. The budget: max_calls=5, and we have now spent all five.
        # ------------------------------------------------------------------
        print(f"3. budget: {bridge.calls_made} used, {bridge.calls_remaining} left")
        refused = runtime.eval("""
          try { tools.get_weather('Zurich'); 'call went through' }
          catch (e) { e.name + ': ' + e.message }
        """)
        print(f"   next call -> {refused}")

        # A new agent turn can start from a clean budget.
        bridge.reset_budget()
        again = runtime.eval('tools.get_weather("Oslo")')
        print(f"   after reset_budget() -> {again}")

    # ----------------------------------------------------------------------
    # 4. Everything the sandbox printed, as structured Python values.
    # ----------------------------------------------------------------------
    print("4. captured console output:")
    for level, args in console:
        print(f"   [{level}] {args}")


if __name__ == "__main__":
    asyncio.run(main())
