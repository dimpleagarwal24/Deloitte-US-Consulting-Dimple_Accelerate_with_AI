# Pre-key Validation Review

The review team provides API keys only after implementation acceptance. This
submission therefore separates key-free implementation evidence from later live
model validation. No API key, personal access token or provider call is needed to
reproduce the checks below. Official acceptance remains the team's decision.

## Mandatory checks

| Check | Implementation and evidence |
| --- | --- |
| A. All agents present and wired | `cli.py` calls profiler, STTM generator, Bronze, Silver, Gold and reporter in order. The completed audit log records every stage. |
| B. Intermediate datasets present | [Completed manifest](evidence/completed/manifest.json) lists 3 Bronze, 3 Silver and 1 Gold Parquet file with row counts, schemas and SHA-256 hashes. The files are retained alongside it. |
| C. Real blocking human approval gates | Production CLI uses blocking input before each layer, supports edit/re-review, and stops on rejection or EOF. Real subprocess tests prove all three pauses and no premature downstream writes. Manual review remains available through `--offline-review`. |

All three checks have implementation and automated offline evidence. The test
harness supplied one stdin response per gate after proving it was blocked. These
are NOT human approval records and NOT a live LLM evaluation. The CLI has no
auto-approve option. Reviewers can demonstrate manual gates without credentials.

## Inspect retained evidence

- [Completed manifest](evidence/completed/manifest.json): run ID, zero API calls,
  blocking assertions, artifact inventory, hashes and Parquet row counts.
- [Completed terminal transcript](evidence/completed/terminal.txt): real subprocess
  output, explicitly labelled as scripted offline testing.
- [Offline HTML report](evidence/completed/reports/report_20261113_071842_a5479121a134.html):
  standalone report with the OFFLINE REVIEW banner; no server required.
- [Report JSON](evidence/completed/reports/report_20261113_071842_a5479121a134.json):
  executed query, result data and execution mode.
- [Bronze rejection](evidence/abort_bronze/manifest.json),
  [Silver rejection](evidence/abort_silver/manifest.json), and
  [Gold rejection](evidence/abort_gold/manifest.json): the rejected layer was not executed.

Within `evidence/completed/`, the `data/` directories contain all intermediate
Parquet files, copied training inputs, profile JSON, the three STTM CSVs and state.
The `audit_logs/` file links all stages and records each approved mapping's hash.
Manifest paths are relative to their scenario directory. Some source metadata and
state paths record the original Windows generation location; use the manifest's
relative paths when inspecting a clone. `.gitattributes` preserves evidence bytes
across checkouts so the recorded hashes remain valid.

The retained category example preserves 186 Bronze sales rows, produces 174 Silver
sales rows after normalization and exact-row deduplication, and returns five Gold
categories. Returns remain negative; reported amounts are not recalculated.

## Reproduce without a key

From the project root after installing `requirements.txt` in the Python 3.12 venv:

```powershell
.\.venv\Scripts\python.exe cli.py --offline-review
```

At each gate inspect the STTM and choose `y`, `e` or `n`. The four checked-in files
under `fixtures/` replace model responses only. Actual profiling, casting,
cleansing, joins/aggregations, Parquet writes, SQL, charts and audit code still run.
The fixed scenario cannot be used with a different question or custom file list.

For automated checks and a new retained evidence directory:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q --cov=core --cov=agents --cov=cli --cov-branch --cov-report=term-missing --cov-report=json --cov-fail-under=80 --review-evidence review/evidence-new
.\.venv\Scripts\python.exe -m ruff check core agents tests cli.py
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pip_audit --disable-pip --no-deps -r requirements.txt
```

Use a new evidence directory on each run; the tests refuse to overwrite existing
scenarios. The dependency audit requires advisory-service network access, but no
model credentials. Ordinary tests and offline CLI execution need no network.

## Local verification

Verified on Windows / Python 3.12.10 on 2026-11-13:

- 131 tests passed; 91.28% combined line/branch coverage after the AWA comparison update.
- All implementation files meet the 80% line coverage threshold.
- Ruff lint and package consistency checks passed.
- Dependency audit found no known vulnerabilities in the pinned package set.
- Category and region totals reconcile against independent pandas calculations.
- Four real subprocess scenarios verify all approval pauses and abort behavior.

The AWA comparison and adopted features are documented in the root README.
Retained evidence files describe the earlier offline run and remain byte-for-byte
unchanged; current tests execute fresh scenarios in isolated temporary directories.

Ubuntu is configured in CI but has not been run locally. Dependency advisory
results can change over time. Live model response quality, endpoint access and
rate limits are deliberately deferred until the team grants API access.

## Submission and revalidation

Include code, tests, root CSV inputs, dependency pins, the workflow, fixtures and
this evidence folder. Exclude `.env`, `.venv`, caches and private credentials.
The root `.gitignore` protects the private environment file; only `.env.example`
belongs in the repository. Do not force-add ignored secrets.

The submission repository is
[Deloitte-US-Consulting-Dimple_Accelerate_with_AI](https://github.com/dimpleagarwal24/Deloitte-US-Consulting-Dimple_Accelerate_with_AI).
This project occupies the repository root, matching the workflow paths. Provide
the submitted commit SHA to the review team. Address feedback and submit follow-up
commits to this same repository. Do not replace the offline evidence with a claim
of live validation; add separate live evidence only after credentials are granted.