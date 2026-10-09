#!/usr/bin/env python3
"""tests/smoke.py runs every scenario: Smoke.run() finds its s<N>_ methods by reflection (scenario_names), in the
order of N, and stops before booting anything when the numbers have a gap or a duplicate. This checks that
discovery over Smoke as it is, that it rejects a gap, a duplicate and a 0, and that the module docstring numbers
the same scenarios."""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smoke  # noqa: E402


class Gap:
    def s1_a(self): pass  # noqa: E704
    def s3_c(self): pass  # noqa: E704


class Duplicate:
    def s1_a(self): pass  # noqa: E704
    def s1_b(self): pass  # noqa: E704
    def s2_c(self): pass  # noqa: E704


class Zero:
    def s0_a(self): pass  # noqa: E704
    def s1_b(self): pass  # noqa: E704


def main():
    names = smoke.scenario_names(smoke.Smoke)
    numbers = [int(smoke.SCENARIO.match(name)[1]) for name in names]
    if not names or numbers != list(range(1, len(names) + 1)):
        sys.exit(f"scenarios: not numbered 1, 2, ...: {names}")
    for cls, problem in ((Gap, "no s2_ scenario"), (Duplicate, "s1_a and s1_b share the number 1"),
                         (Zero, "s0_a is numbered below 1")):
        try:
            smoke.scenario_names(cls)
        except smoke.TestFailure as e:
            if problem not in str(e):
                sys.exit(f"scenarios: {cls.__name__} rejected for another reason: {e}")
        else:
            sys.exit(f"scenarios: {cls.__name__} was accepted")
    documented = [int(n) for n in re.findall(r"^  (\d+)\. ", smoke.__doc__, re.M)]
    if documented != numbers:
        sys.exit(f"scenarios: the docstring numbers {documented}, the methods {numbers}")
    print(f"scenarios: {len(names)} in order: {' '.join(names)}")


if __name__ == "__main__":
    main()
