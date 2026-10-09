"""Order-scoped manifest executor backed by darwin_seatbelt."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import omnigent.sandbox.seatbelt  # noqa: F401 — register darwin_seatbelt backend
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.sandbox import (
    cleanup_private_tmpdir,
    create_private_tmpdir,
    get_backend,
    set_sandbox_env,
    with_additional_write_roots,
    with_spawn_env_allowlist,
)
from omnigent.inner.seatbelt_sandbox import SeatbeltSandboxBackend

from .manifest import OrderManifest
from .validate import validate_manifest_before_spawn


@dataclass
class ChildRunResult:
    returncode: int
    stdout: str
    stderr: str
    manifest: OrderManifest


class OrderScopedManifestExecutor:
    """
    Spawn one closed argv under Seatbelt for a single order id.

    Uses :meth:`SeatbeltSandboxBackend.wrap_launcher_argv` directly so the
    child does not need to import ``omnigent`` (the checkout lives outside the
    package tree). Fixture-only — not wired into the live runtime.
    """

    def __init__(
        self,
        *,
        order_id: str,
        checkout: Path,
        child_script: Path,
        python_executable: Path | None = None,
    ) -> None:
        self._order_id = order_id
        self._checkout = checkout.resolve(strict=False)
        self._child_script = child_script.resolve(strict=False)
        self._python = (python_executable or Path(sys.executable)).resolve(strict=False)
        self._last_order_id: str | None = None

    @property
    def sandbox_backend(self) -> str:
        return "darwin_seatbelt"

    def build_manifest(self, verb: str, extra_args: tuple[str, ...] = ()) -> OrderManifest:
        argv = (
            str(self._python),
            str(self._child_script),
            verb,
            *extra_args,
        )
        return OrderManifest(
            order_id=self._order_id,
            verb=verb,
            argv=argv,
            cwd=str(self._checkout),
        )

    def _resolve_policy(self) -> object:
        spec = OSEnvSpec(
            sandbox=OSEnvSandboxSpec(
                type="darwin_seatbelt",
                write_paths=["."],
                allow_network=False,
                env_passthrough=[],
            )
        )
        policy = SeatbeltSandboxBackend().resolve(spec, self._checkout)
        return with_spawn_env_allowlist(policy, [])

    @staticmethod
    def _launcher_env(scratch: Path) -> dict[str, str]:
        path = os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        env: dict[str, str] = {"PATH": path, "LC_ALL": "C", "LANG": "C"}
        set_sandbox_env(env, scratch)
        return env

    def execute(self, manifest: OrderManifest, *, timeout_seconds: float = 120.0) -> ChildRunResult:
        validate_manifest_before_spawn(
            manifest,
            bound_order_id=self._order_id,
            expected_cwd=self._checkout,
            expected_child_script=self._child_script,
            expected_python=self._python,
            prior_order_id=self._last_order_id,
        )
        self._last_order_id = manifest.order_id

        scratch = create_private_tmpdir()
        policy = with_additional_write_roots(self._resolve_policy(), [scratch])
        backend = get_backend("darwin_seatbelt")
        argv = list(manifest.argv)
        # Trusted probe lives outside the writable checkout; grant read only for that script.
        trusted_script = self._child_script.resolve(strict=False)
        checkout = self._checkout.resolve(strict=False)
        launcher_read_literals: list[str] = []
        try:
            trusted_script.relative_to(checkout)
        except ValueError:
            launcher_read_literals = [str(trusted_script)]
        wrapped = backend.wrap_launcher_argv(
            argv,
            policy,
            checkout,
            launcher_read_literals=launcher_read_literals or None,
        )

        env = self._launcher_env(scratch)
        polluted_parent = False
        if manifest.verb == "probe_env_secret":
            # Simulate a polluted parent environment without passing it through spawn env=.
            os.environ["FIXTURE_PROBE_ENV_SECRET"] = "parent-only-synthetic-value"
            polluted_parent = True

        try:
            completed = subprocess.run(
                wrapped,
                cwd=str(self._checkout),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        finally:
            if polluted_parent:
                os.environ.pop("FIXTURE_PROBE_ENV_SECRET", None)
            cleanup_private_tmpdir(scratch)

        return ChildRunResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            manifest=manifest,
        )


def decode_child_payload(stdout: str) -> dict[str, object]:
    line = stdout.strip().splitlines()[-1] if stdout.strip() else ""
    if not line:
        raise ValueError("child produced no JSON payload")
    data = json.loads(line)
    if not isinstance(data, dict):
        raise ValueError("child payload must be a JSON object")
    return data
