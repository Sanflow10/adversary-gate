#!/usr/bin/env python3
"""AdversaryGate — demonstração ao vivo E teste de aceitação.

Três cenários sobre o MESMO repositório:

    A  patch quebra o teste              -> BLOCK         (exit 1)
    B  patch limpo, SEM evidência        -> INCONCLUSIVE  (exit 2)
    C  patch limpo, COM evidência        -> MERGE         (exit 0)

B e C são idênticos: mesmo código, mesmos testes, mesma execução.
A única diferença é que C declara `--diff` e `--coverage-json`.

Se qualquer cenário não produzir a decisão esperada, isto falha —
ou seja, este script é também um teste.

Uso:
    python3 demo/demo.py                 # transcript no terminal
    python3 demo/demo.py --json out.json # frames estruturados p/ renderizador
    python3 demo/demo.py --no-color       # saída sem ANSI
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "src" / "cli.py"

B, R, DIM = "\033[1m", "\033[0m", "\033[2m"
RED, GREEN, YELLOW, CYAN, GREY, WHITE = (
    "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[90m", "\033[37m",
)

BASE_CALC = "def add(a, b):\n    return a + b\n"
BROKEN_CALC = "def add(a, b):\n    return a - b\n"
CLEAN_CALC = "def add(a, b):\n    return (a + b)\n"
TEST_SUM = "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"

# The only added line in the clean patch is line 2 of calc.py.
CLEAN_DIFF = (
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n"
    "-    return a + b\n"
    "+    return (a + b)\n"
)

# Line 2 executed, and only line 2 was added -> measured coverage is 1.0.
COVERAGE_JSON = json.dumps({"files": {"calc.py": {"executed_lines": [1, 2]}}})

EXPECTED = {"A": ("BLOCK", 1), "B": ("INCONCLUSIVE", 2), "C": ("MERGE", 0)}

_ANSI = (
    "\033[0m", "\033[1m", "\033[2m", "\033[31m", "\033[32m",
    "\033[33m", "\033[36m", "\033[37m", "\033[90m",
)

lines: list[tuple[str, str]] = []  # (text, ansi_prefix)


def strip_ansi(text: str) -> str:
    for code in _ANSI:
        text = text.replace(code, "")
    return text


def emit(text: str = "", style: str = "") -> None:
    lines.append((text, style))


def build_fixtures(root: Path) -> None:
    for side in ("baseline", "patch_clean", "patch_broken"):
        d = root / side
        d.mkdir(parents=True, exist_ok=True)
        (d / "calc.py").write_text(BASE_CALC)
        (d / "test_sum.py").write_text(TEST_SUM)
    (root / "patch_clean" / "calc.py").write_text(CLEAN_CALC)
    (root / "patch_broken" / "calc.py").write_text(BROKEN_CALC)
    (root / "change.diff").write_text(CLEAN_DIFF)
    (root / "coverage.json").write_text(COVERAGE_JSON)


def run_gate(args: list[str], cwd: Path) -> tuple[int, dict, float, Path | None]:
    """Run the CLI exactly as CI does: a subprocess, reading its exit code."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    log = cwd / "evidence.jsonl"
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(CLI), *args, "--evidence-log", str(log)],
        capture_output=True, text=True, env=env, cwd=str(cwd),
    )
    elapsed = time.monotonic() - started
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"_error": proc.stdout + proc.stderr}
    return proc.returncode, payload, elapsed, log if log.exists() else None


def scenario(letter: str, label: str, patch: str, extra: list[str], root: Path) -> dict:
    emit()
    emit(f"{'=' * 74}", GREY)
    emit(f"  CENÁRIO {letter} — {label}", f"{B}{WHITE}")
    emit(f"{'=' * 74}", GREY)

    args = [
        "--baseline", str(root / "baseline"),
        "--patch", str(root / patch),
        "--test-path", "test_sum.py",
        "--test-id", "test_add",
        *extra,
    ]
    emit(f"  $ adversary-gate \\", GREY)
    for i in range(0, len(args), 2):
        emit(f"      {' '.join(args[i:i + 2])}", GREY)

    code, payload, elapsed, log_path = run_gate(args, root)

    decision = payload.get("decision", "?")
    colour = {"merge": GREEN, "block": RED, "inconclusive": YELLOW}.get(
        decision.lower(), WHITE)

    emit()
    emit(f"    exit code   : {code}", WHITE)
    emit(f"    decision    : {decision}", f"{B}{colour}")
    emit(f"    outcome     : {payload.get('outcome', '?')}", WHITE)
    emit(f"    coverage    : {payload.get('diff_coverage_ratio')!r}"
         f"   (source={payload.get('diff_coverage_source')})", WHITE)
    emit(f"    força suíte : {payload.get('suite_strength')!r}"
         f"   (medida={not payload.get('suite_strength_unverified')})", WHITE)
    emit(f"    suíte total : ran={payload.get('full_suite_ran')}", WHITE)
    emit(f"    tempo       : {elapsed:.1f}s", GREY)

    ev = None
    if log_path is not None:
        for raw in log_path.read_text().splitlines():
            rec = json.loads(raw)
            if rec.get("kind") == "decision":
                ev = rec
    return {
        "letter": letter, "label": label, "exit": code, "decision": decision,
        "payload": payload, "evidence": ev, "seconds": elapsed,
    }


def show_evidence(results: dict[str, dict]) -> None:
    """The artefact: numbers that can be recomputed, not asserted."""
    ev = results["C"]["evidence"]
    if ev is None:
        return
    emit()
    emit(f"{'=' * 74}", GREY)
    emit(f"  ARTEFATO DE EVIDÊNCIA — cenário C (demo/out/evidence_c.json)", f"{B}{WHITE}")
    emit(f"{'=' * 74}", GREY)
    emit()
    emit(f"  {B}diff_coverage_ratio{R}   = {ev.get('diff_coverage_ratio')}", WHITE)
    emit(f"  {B}diff_coverage_source{R} = {ev.get('diff_coverage_source')}", WHITE)
    det = ev.get("diff_coverage") or {}
    emit(f"  {B}changed/covered{R}      = {det.get('covered_lines')}/{det.get('changed_lines')}"
         f" linhas adicionadas executadas", WHITE)
    emit(f"  {B}diff_sha256{R}          = {det.get('diff_sha256')}", CYAN)
    emit(f"  {B}coverage_json_sha256{R} = {det.get('coverage_json_sha256')}", CYAN)
    emit()
    mut = ev.get("mutation") or {}
    emit(f"  {B}suite_strength{R}       = {ev.get('suite_strength')}"
         f"  (floor {ev.get('suite_strength_floor')})", WHITE)
    emit(f"  {B}mutantes{R}             = {mut.get('mutants_killed')}/{mut.get('mutants_counted')}"
         f" mortos, {mut.get('stillborn')} stillborn", WHITE)
    emit(f"  {B}changed_files{R}        = {mut.get('changed_files')}", WHITE)
    emit(f"  {B}deleted_files{R}        = {mut.get('deleted_files')}", WHITE)
    emit()
    emit(f"  {B}full_suite_ran{R}       = {ev.get('full_suite_ran')}", WHITE)
    codes = ev.get("full_suite_exit_codes")
    if codes is None:
        meaning = "rodou; lado patch passou"
    else:
        meaning = f"baseline={codes[0]}, patch={codes[1]}"
    emit(f"  {B}full_suite_exit_codes{R} = {codes}  -> {meaning}", WHITE)
    emit(f"  {B}rounds_used{R}          = {ev.get('rounds_used')}", WHITE)
    emit()
    emit(f"  {DIM}Nada aqui é uma opinião de um modelo. Cada valor pode ser", GREY)
    emit(f"  {DIM}re-derivado a partir dos dois hashes acima.{R}", GREY)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", metavar="PATH", help="grava frames estruturados")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--keep", action="store_true", help="não apaga os fixtures")
    opts = ap.parse_args()

    if not CLI.is_file():
        print(f"CLI não encontrada: {CLI}", file=sys.stderr)
        return 3

    root = Path(tempfile.mkdtemp(prefix="ag-demo-"))
    out = REPO / "demo" / "out"
    out.mkdir(parents=True, exist_ok=True)

    build_fixtures(root)

    emit(f"{B}AdversaryGate v2.0.2 — demonstração ao vivo{R}", B)
    emit(f"repositório: {REPO}", GREY)
    emit()
    emit(f"  Três cenários. B e C são {B}o mesmo patch{R}: mesmo código, mesmos")
    emit(f"  testes, mesma execução. A única diferença é que C trouxe evidência.")

    results = {
        "A": scenario("A", "patch quebra o teste", "patch_broken",
                      ["--coverage-source", "untrusted", "--coverage-ratio", "1.0"], root),
        "B": scenario("B", "patch limpo, SEM evidência de cobertura", "patch_clean", [], root),
        "C": scenario("C", "patch limpo, COM evidência de cobertura", "patch_clean",
                      ["--diff", str(root / "change.diff"),
                       "--coverage-json", str(root / "coverage.json")], root),
    }

    show_evidence(results)

    emit()
    emit(f"{'=' * 74}", GREY)
    emit(f"  RESULTADO", f"{B}{WHITE}")
    emit(f"{'=' * 74}", GREY)
    ok = True
    for letter in ("A", "B", "C"):
        got = (results[letter]["decision"].lower(), results[letter]["exit"])
        want = EXPECTED[letter]
        good = got == (want[0].lower(), want[1])
        ok = ok and good
        mark = f"{GREEN}PASS{R}" if good else f"{RED}FAIL{R}"
        emit(f"  [{mark}]  {letter}  esperado {want[0]:<13} obtido {got[0]:<13} exit {got[1]}")
    emit()
    emit(f"  B e C: {B}mesmo patch, decisões diferentes{R}.")
    emit(f"  A diferença é apenas se houve evidência.")
    emit()
    emit(f"  {'TODOS OS CENÁRIOS CONFIRMADOS' if ok else 'FALHA EM ALGUM CENÁRIO'}",
         f"{B}{GREEN if ok else RED}{R}")

    printable = [(strip_ansi(t), "") for t, _ in lines] if opts.no_color else lines

    for text, style in printable:
        print(f"{style}{text}{R}" if style else text)

    if opts.json:
        Path(opts.json).write_text(json.dumps(
            {"title": "AdversaryGate v2.0.2 — demonstração ao vivo",
             "lines": [{"t": t, "s": s} for t, s in lines],
             "ok": ok}, ensure_ascii=False, indent=1))
        print(f"\nframes -> {opts.json}", file=sys.stderr)

    # Persist the artefacts that the demo produced, for the README.
    for letter in ("A", "B", "C"):
        ev = results[letter]["evidence"]
        if ev is not None:
            (out / f"evidence_{letter.lower()}.json").write_text(
                json.dumps(ev, ensure_ascii=False, indent=2))
    (out / "transcript.txt").write_text(
        "".join(strip_ansi(t) + "\n" for t, _ in lines))

    if not opts.keep:
        shutil.rmtree(root, ignore_errors=True)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
