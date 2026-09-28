"""Daily login: ICICI Breeze first, then Zerodha Kite.

    python3 -m login                      # Breeze (browser + OTP), then Zerodha (browser redirect)
    python3 -m login --breeze-manual      # paste the Breeze redirect URL instead of browser automation
    python3 -m login --zerodha-manual     # paste the Kite redirect URL instead of catching it
    python3 -m login --continue-on-error  # still run Zerodha if Breeze fails
    python3 -m login --dry-run            # only print the commands that would run

Runs the existing flows unchanged, each in a child process of this interpreter:
    python3 scripts/get_session_token.py
    python3 -m zerodha login
Run from the repo root (the wrapper sets the working directory itself).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build_steps(args: argparse.Namespace) -> list[tuple[str, list[str]]]:
    breeze = [sys.executable, str(ROOT / "scripts" / "get_session_token.py")]
    if args.breeze_manual:
        breeze.append("--manual")
    zerodha = [sys.executable, "-m", "zerodha", "login"]
    if args.zerodha_manual:
        zerodha.append("--manual")
    return [("Breeze", breeze), ("Zerodha", zerodha)]


def run_steps(steps: list[tuple[str, list[str]]], continue_on_error: bool, runner=subprocess.call) -> dict[str, str]:
    """Run each step in order; stop at the first failure unless continue_on_error. Returns name -> status."""
    results: dict[str, str] = {name: "SKIPPED" for name, _ in steps}
    for name, cmd in steps:
        print(f"\n=== {name} login ===", flush=True)
        try:
            code = runner(cmd, cwd=ROOT)
        except KeyboardInterrupt:
            code = 130
        results[name] = "OK" if code == 0 else f"FAILED (exit {code})"
        if code != 0 and not continue_on_error:
            print(f"\n{name} login failed; not continuing (use --continue-on-error to run the rest).")
            break
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m login", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--breeze-manual", action="store_true", help="Breeze: paste the redirected URL (no automation)")
    ap.add_argument("--zerodha-manual", action="store_true", help="Zerodha: paste the redirect URL")
    ap.add_argument("--continue-on-error", action="store_true", help="run Zerodha even if Breeze fails")
    ap.add_argument("--dry-run", action="store_true", help="print the commands without running them")
    args = ap.parse_args(argv)

    steps = build_steps(args)
    if args.dry_run:
        for name, cmd in steps:
            print(f"{name}: (cd {ROOT} && {' '.join(cmd)})")
        return 0

    results = run_steps(steps, args.continue_on_error)
    print("\n=== Login summary ===")
    for name, status in results.items():
        print(f"  {name:<8} {status}")
    return 0 if all(s == "OK" for s in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
