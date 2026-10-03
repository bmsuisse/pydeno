"""Optional integrations with third-party frameworks.

Each submodule imports its framework at import time, so import it only when that framework is
installed: ``from pydeno.integrations.pydantic_ai import JSCodeMode``. Importing ``pydeno`` (or
this package) never imports any of them.
"""
