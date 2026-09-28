import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from login.__main__ import ROOT, build_steps, main, run_steps  # noqa: E402


def _args(argv):
    import argparse
    ns = argparse.Namespace(breeze_manual="--breeze-manual" in argv, zerodha_manual="--zerodha-manual" in argv)
    return ns


def test_steps_target_existing_flows_in_order():
    steps = build_steps(_args([]))
    assert [name for name, _ in steps] == ["Breeze", "Zerodha"]
    assert steps[0][1][1] == str(ROOT / "scripts" / "get_session_token.py")
    assert (ROOT / "scripts" / "get_session_token.py").exists()
    assert steps[1][1][1:] == ["-m", "zerodha", "login"]
    assert all(cmd[0] == sys.executable for _, cmd in steps)


def test_manual_flags_pass_through():
    steps = build_steps(_args(["--breeze-manual", "--zerodha-manual"]))
    assert steps[0][1][-1] == "--manual" and steps[1][1][-1] == "--manual"


def test_breeze_failure_stops_before_zerodha():
    calls = []
    codes = iter([1, 0])
    results = run_steps(build_steps(_args([])), False, lambda cmd, cwd: calls.append(cmd) or next(codes))
    assert len(calls) == 1
    assert results == {"Breeze": "FAILED (exit 1)", "Zerodha": "SKIPPED"}


def test_continue_on_error_runs_both():
    codes = iter([1, 0])
    results = run_steps(build_steps(_args([])), True, lambda cmd, cwd: next(codes))
    assert results == {"Breeze": "FAILED (exit 1)", "Zerodha": "OK"}


def test_all_ok_and_dry_run(capsys):
    results = run_steps(build_steps(_args([])), False, lambda cmd, cwd: 0)
    assert results == {"Breeze": "OK", "Zerodha": "OK"}
    assert main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "get_session_token.py" in out and "-m zerodha login" in out
