"""
Harness package — per-conversation subprocesses that implement a
subset of the Omnigent REST API.

See ``designs/SERVER_HARNESS_CONTRACT.md`` for the full contract.
The harness IS an HTTP service speaking the same Pydantic models AP
serves to external clients (re-use ``omnigent.server.schemas`` —
there is no separate protocol module).

This package contains:

- ``_HARNESS_MODULES``: registry mapping harness name (the value of
  ``spec.executor.harness`` in an agent spec) to the fully-qualified
  Python module path that exports a zero-argument ``create_app() ->
  FastAPI``. Populated as per-harness wraps land (Phase 1 step 4).
- ``process_manager``: ``HarnessProcessManager`` — owns
  per-conversation subprocess lifecycle.
- ``_runner``: shared ``python -m`` entrypoint that any registered
  harness's ``create_app()`` is served through.

The package directory is intentionally small. Behavior lives in the
sibling modules; this ``__init__.py`` is just the registry.
"""

from __future__ import annotations

from dev.factory.order_scoped.binding import STAGE_WORKER_ENV
from omnigent.factory.gate_a.admission import FACTORY_GATE_A_CURSOR_CLI_ENV, factory_gate_a_enabled
from omnigent.factory.gate_a.real_chat import factory_gate_a_real_enabled
from omnigent.harness_plugins import harness_modules

# Harness-name -> fully-qualified module path, sourced from the harness
# registry (built-ins + installed community plugins). Each module must
# export ``create_app() -> FastAPI``; the runner imports the module, calls
# the factory, and serves the result over a Unix socket. The historical
# mutable-dict surface is preserved (tests inject fixture entries by dict
# mutation), but the contents come from the dynamic registry, not a literal.

# Keep the historical mutable dict surface while sourcing builtins and
# community plugins from the dynamic registry.
_HARNESS_MODULES = harness_modules()

_FACTORY_GATE_A_MODULE = "omnigent.inner.factory_gate_a_harness"
if factory_gate_a_enabled():
    _HARNESS_MODULES.setdefault("factory-gate-a", _FACTORY_GATE_A_MODULE)

_FACTORY_GATE_A_REAL_MODULE = "omnigent.inner.factory_gate_a_real_harness"
if factory_gate_a_real_enabled():
    _HARNESS_MODULES.setdefault("factory-gate-a-real", _FACTORY_GATE_A_REAL_MODULE)

__all__ = ["FACTORY_GATE_A_CURSOR_CLI_ENV", "STAGE_WORKER_ENV", "_HARNESS_MODULES"]
