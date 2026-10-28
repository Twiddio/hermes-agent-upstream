"""PROBE ONLY (wine2e-install branch, never merges): hammer the desktop_handoff crash cell.

Install once, then repeat the real ``desktop_handoff`` cell (hand-off script whole tree killed
once its ``hermes update`` child exists, next launch, follow-up update) until the time budget
runs out. Each round records whether the next launch was refused ``update_paused``, the
product's own fence diagnosis (logs/fence-diag.log, probe-only instrumentation), and a 50 ms
marker sample series from this process. One ``PROBE {json}`` line per round, a summary line.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import psutil
import pytest

from hermes_cli import update_lock
from tests.e2e.core.windows_update._machine import (
    REQUIRES_OPT_IN, UPDATE_TIMEOUT, new_machine, one_shot_turn,
)
from tests.e2e.core.windows_update.test_crash_cells import (
    MARKER, RUNTIME_FILES, _handoff, _head, _kill_when, _update_child,
)
from tests.fakes.fake_llm_provider import FakeLLMServer

pytestmark = [pytest.mark.platforms("windows"), pytest.mark.integration,
              pytest.mark.live_system_guard_bypass, REQUIRES_OPT_IN]

BUDGET = float(os.environ.get("PROBE_BUDGET_SECONDS", "2400"))


def _sample(machine) -> str:
    path = machine.hermes_home / MARKER
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        return f"unreadable:{exc.winerror if hasattr(exc, 'winerror') else exc}"
    if not raw:
        return "empty"
    verdict, owner, _ = update_lock.judge_marker(raw)
    if verdict in {"live", "ours"} and owner:
        try:
            p = psutil.Process(owner)
            return f"{verdict}:{owner}:{p.name()}:{p.create_time():.3f}"
        except psutil.Error:
            return f"{verdict}:{owner}:?"
    return verdict


def _round(machine, srv, i: int) -> dict:
    label = f"probe{i:02d}"
    pre = _head(machine)
    target = machine.mint(pre, label, RUNTIME_FILES)
    machine.publish(target)
    diag = machine.hermes_home / "logs" / "fence-diag.log"
    diag_before = diag.read_text(encoding="utf-8", errors="replace").count("\n") if diag.is_file() else 0
    with machine.gateway_phase():
        proc = _handoff(machine, f"{label}-script")()
        marker_at_kill = None
        try:
            seen = _kill_when(machine, proc, label, _update_child, target)
        except AssertionError as exc:
            return {"round": i, "harness": str(exc).splitlines()[0][:300]}
        killed = time.monotonic()
        marker_at_kill = (machine.hermes_home / MARKER).read_bytes().decode("utf-8", "replace") \
            if (machine.hermes_home / MARKER).is_file() else None
        lock_after_kill = update_lock.checkout_lock_held(machine.install_dir)
        samples: list[tuple[float, str]] = []
        stop = threading.Event()

        def poll():
            last = None
            while not stop.is_set():
                s = _sample(machine)
                if s != last:
                    samples.append((round(time.monotonic() - killed, 2), s))
                    last = s
                time.sleep(0.1)

        t = threading.Thread(target=poll, daemon=True)
        t.start()
        turn = one_shot_turn(machine, srv, f"{label}-next-launch")
        stop.set()
        t.join(5)
        refused = "update_paused" in turn.run.stdout
        diag_lines = []
        if diag.is_file():
            diag_lines = diag.read_text(encoding="utf-8", errors="replace").splitlines()[diag_before:]
        follow = machine.hermes("update", "--yes", label=f"{label}-follow-up-update", timeout=UPDATE_TIMEOUT)
    return {"round": i, "seen": seen, "refused": refused, "ok": turn.ok, "marker_at_kill": marker_at_kill,
            "lock_after_kill": lock_after_kill, "samples": samples[:40], "fence_diag": diag_lines[:6],
            "follow_rc": follow.returncode}


def test_handoff_fence_probe(tmp_path_factory):
    results = []
    with FakeLLMServer() as srv:
        machine = new_machine(tmp_path_factory.mktemp("probe"), srv.base_url, label="probe")
        try:
            run = machine.install()
            assert run.returncode == 0, "install.ps1 failed"
            started = time.monotonic()
            i = 0
            while time.monotonic() - started < BUDGET:
                r = _round(machine, srv, i)
                results.append(r)
                print("PROBE " + json.dumps(r), flush=True)
                i += 1
        finally:
            artifacts = os.environ.get("HERMES_E2E_ARTIFACTS")
            if artifacts:
                Path(artifacts).mkdir(parents=True, exist_ok=True)
                (Path(artifacts) / "probe-results.jsonl").write_text(
                    "".join(json.dumps(r) + "\n" for r in results), encoding="utf-8")
            machine.teardown()
    refused = [r["round"] for r in results if r.get("refused")]
    print(f"PROBE-SUMMARY rounds={len(results)} refused={len(refused)} {refused}", flush=True)
    assert not refused, f"{len(refused)}/{len(results)} next launches refused update_paused: {refused}"
