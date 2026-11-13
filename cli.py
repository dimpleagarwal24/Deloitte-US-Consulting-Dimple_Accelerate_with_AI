import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path

import pandas as pd
from tabulate import tabulate

from agents import bronze_agent, gold_agent, profiler, reporter, silver_agent, sttm_generator
from core.audit import AuditLogger, new_run_id
from core.config import BASE_DIR, LANDING_DIR, TRACES_DIR, ensure_dirs
from core.llm import LLMError, fixture_responses, response_source
from core.mappings import identifier, read_mapping
from core.state import PipelineState


def banner(text: str) -> None:
    print(f"\n=== {text} ===")


def display_sttm(sttm_path: str, layer: str) -> None:
    mapping = read_mapping(sttm_path, layer.lower())
    print(tabulate(mapping, headers="keys", tablefmt="rounded_outline", showindex=False))
    print(f"\nMapping: {sttm_path}")


def edit_sttm(sttm_path: str) -> None:
    editor = os.getenv("EDITOR")
    command = [part.strip('"') for part in shlex.split(editor, posix=os.name != "nt")] if editor else ["notepad" if os.name == "nt" else "nano"]
    subprocess.run([*command, str(Path(sttm_path).resolve())], check=True, shell=False)


def hitl_gate(layer: str, sttm_path: str) -> bool:
    banner(f"Review {layer.title()} mapping")
    while True:
        valid = True
        try:
            display_sttm(sttm_path, layer)
        except (ValueError, OSError, pd.errors.ParserError) as error:
            print(f"Mapping is invalid: {error}")
            valid = False
        try:
            answer = input("[y]es / [e]dit then re-review / [n]o abort > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nApproval not received; aborting.")
            return False
        if answer in {"y", "yes"} and valid:
            return True
        if answer in {"n", "no"}:
            return False
        if answer in {"e", "edit"}:
            try:
                edit_sttm(sttm_path)
            except (OSError, subprocess.CalledProcessError):
                print("Editor could not be opened. Edit the mapping file manually, then re-review.")


def _checkpoint(state: PipelineState) -> None:
    (TRACES_DIR / f"state_{state.run_id}.json").write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")


def _approve(layer: str, path: str, state: PipelineState, audit: AuditLogger) -> bool:
    state.status = f"awaiting_{layer}_approval"
    _checkpoint(state)
    accepted = hitl_gate(layer, path)
    audit.log("approval", "approved" if accepted else "aborted", layer=layer,
              mapping_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest() if accepted else None)
    if not accepted:
        state.status = "aborted"
        _checkpoint(state)
    return accepted


def run_pipeline(files: list[str], intent: str) -> PipelineState:
    if not intent.strip() or len(intent) > 2000:
        raise ValueError("Business intent must contain 1 to 2000 characters")
    sources = [Path(path).resolve() for path in files]
    if not sources or any(not path.is_file() or path.suffix.lower() != ".csv" for path in sources):
        raise ValueError("Provide existing CSV files")
    if len({path.stem.lower() for path in sources}) != len(sources):
        raise ValueError("Input filenames must be unique")
    for path in sources:
        identifier(path.stem)
    ensure_dirs()
    run_id = new_run_id()
    audit = AuditLogger(run_id)
    state = PipelineState(run_id=run_id, business_intent=intent.strip(),
                          execution_mode="offline_review" if response_source() == "review_fixture" else "live")
    try:
        landing = LANDING_DIR / run_id
        landing.mkdir(exist_ok=False)
        for source in sources:
            destination = landing / source.name
            shutil.copy2(source, destination)
            state.input_files.append(str(destination))
        audit.log("pipeline", "started", input_count=len(sources), execution_mode=state.execution_mode)
        _checkpoint(state)
        banner(f"Profile | run {run_id}")
        state.profile_path = profiler.profile(state.input_files, run_id)
        state.bronze_sttm_path = sttm_generator.generate_bronze_sttm(state.profile_path, intent, run_id)
        if not _approve("bronze", state.bronze_sttm_path, state, audit):
            return state
        state.bronze_paths = bronze_agent.run(state.input_files, state.bronze_sttm_path, run_id)
        state.silver_sttm_path = sttm_generator.generate_silver_sttm(state.bronze_paths, state.bronze_sttm_path, intent, run_id)
        if not _approve("silver", state.silver_sttm_path, state, audit):
            return state
        state.silver_paths = silver_agent.run(state.bronze_paths, state.silver_sttm_path, run_id)
        state.gold_sttm_path = sttm_generator.generate_gold_sttm(state.silver_paths, state.silver_sttm_path, intent, run_id)
        if not _approve("gold", state.gold_sttm_path, state, audit):
            return state
        state.gold_paths = gold_agent.run(state.silver_paths, state.gold_sttm_path, intent, run_id)
        state.report_path = reporter.generate_report(state.gold_paths, intent, run_id)
        state.status = "completed"
        _checkpoint(state)
        audit.log("pipeline", "completed", report_path=state.report_path)
        banner("Complete")
        print(f"Report: {state.report_path}\nAudit: {audit.log_path}")
        return state
    except (Exception, KeyboardInterrupt) as error:
        state.status = "failed"
        _checkpoint(state)
        audit.log("pipeline", "failed", error_type=type(error).__name__)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Intent-driven CSV to Bronze/Silver/Gold analytics with three approval gates.")
    parser.add_argument("--files", nargs="+", help="CSV input paths")
    parser.add_argument("--intent", help="Business question")
    parser.add_argument("--offline-review", action="store_true", help="Replay the supplied category-revenue fixtures without API keys; all three approvals remain mandatory")
    args = parser.parse_args(argv)
    if args.offline_review and (args.files or args.intent):
        parser.error("--offline-review uses the supplied CSVs and fixed category-revenue question; omit --files and --intent")
    try:
        if args.offline_review:
            banner("OFFLINE REVIEW: fixture responses, no API requests; approvals are still required")
            fixture_dir = BASE_DIR / "review" / "fixtures"
            responses = {stage: (fixture_dir / filename).read_text(encoding="utf-8") for stage, filename in
                         {"bronze": "bronze.csv", "silver": "silver.csv", "gold": "gold.csv", "reporter": "reporter.txt"}.items()}
            with fixture_responses(responses):
                state = run_pipeline([str(BASE_DIR / name) for name in ("sales_data.csv", "products.csv", "stores.csv")],
                                     "Which product category generated the highest net sales revenue?")
        else:
            files = args.files or [part.strip('"') for part in shlex.split(input("CSV paths (quote paths with spaces): "), posix=os.name != "nt")]
            intent = args.intent or input("Business question: ")
            state = run_pipeline(files, intent)
        return 0 if state.status == "completed" else 1
    except (LLMError, ValueError, OSError, pd.errors.ParserError) as error:
        print(f"Pipeline stopped: {error}")
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nPipeline cancelled.")
        return 1
    except Exception:
        print("Pipeline failed unexpectedly. Check the run state and audit log for the failing stage and error type.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())