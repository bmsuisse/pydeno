"""llm-pydeno: a sandboxed JavaScript interpreter for `llm`, backed by pydeno.

The `llm` hooks live in `llm_pydeno.plugin` (the package's ``llm`` entry point), so importing
`llm_pydeno` or `llm_pydeno.session` does not need `llm` installed.
"""

from .session import JavaScriptSession

__all__ = ["JavaScriptSession"]
