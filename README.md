# Medallion Pipeline

A local CLI that turns CSV files and a business question into an auditable
Bronze/Silver/Gold pipeline and a standalone HTML analytics report.
GitHub Models proposes mapping rules; humans approve them; Python executes them.

Status: ready for key-free code review. The team supplies API keys only after
implementation acceptance, so pre-key validation uses an explicit offline review
mode and tests. No credentials are required for that review. Live GitHub Models
behavior remains unverified until the team supplies access. No Streamlit app is included.

See [review/README.md](review/README.md) for the mandatory-check evidence, including
retained intermediate datasets and real CLI blocking-gate tests.

## Quickstart

Run these commands from this `medallion-pipeline` folder. The dependency snapshot
was tested on Python 3.12.10 on Windows. The training guide's older Python 3.10+
baseline has not been validated with these resolved package versions; use Python 3.12.

```powershell
# Only create the environment if it does not exist.
if (-not (Test-Path .venv)) { py -3.12 -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Pre-key review: fixed category-revenue scenario, no network/model requests.
.\.venv\Scripts\python.exe cli.py --offline-review
```

Offline review reads the supplied CSVs and replays four hand-authored response
fixtures from `review/fixtures/`. All agents execute their normal code paths and
all three approval gates still block. Approve, edit or reject each mapping yourself.
There is no auto-approve flag and no silent offline fallback. This mode is limited
to the supplied files and category-revenue question; custom `--files` or `--intent`
arguments are rejected. Reports, state and audit logs identify offline runs.

The checked-in `review/evidence/` contains automated offline test outputs, not
human sign-off or evidence of live model quality. Generated quantities and totals
come from the input data, not canned report numbers. Manifests include artifact
hashes, row counts and proof that downstream layers were absent before each input.

## Live run after acceptance

Only configure credentials when the review team supplies them. Never put tokens
in chat, source code, fixtures, CI logs or submission evidence.

```powershell

# Never replace an existing private environment file.
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

Set `LLM_PROVIDER=github`, enter your token privately as `GITHUB_TOKEN`, and use
`GITHUB_MODEL=openai/gpt-4.1-mini`. A Copilot subscription does not automatically
configure the application's API access. The existing private `.env` is not migrated
or overwritten by this implementation; change an old Anthropic setting yourself.

```powershell
.\.venv\Scripts\python.exe cli.py --files products.csv sales_data.csv stores.csv --intent "Which product category generated the highest net sales revenue?"
```

At each mapping gate, choose `y` to approve, `e` to edit and re-review, or `n` to
abort. Set `EDITOR` to an editor command that waits for edits, such as `code --wait`,
or use the Notepad fallback. Invalid mapping syntax cannot be approved. Semantic
checks such as column existence and reference-key uniqueness also run during execution.
An abort or failure returns exit code 1 and never executes an unapproved layer.

Open the printed HTML report path directly in a browser; no server is required.
For a second question, try `How does net sales revenue break down by region?`.

## Architecture and artifacts

1. Copy inputs into `data/landing/<run_id>/`, leaving originals untouched.
2. Profile inputs into `data/profiles/profile_combined_<run_id>.json`.
3. Generate a Bronze STTM, obtain approval, and cast/write Bronze Parquet.
4. Generate a Silver STTM, obtain approval, and cleanse/write Silver Parquet.
5. Generate a Gold STTM, obtain approval, and join/aggregate/write Gold Parquet.
6. Generate report SQL and chart specifications; run restricted DuckDB SQL and
     render self-contained HTML plus a machine-readable JSON summary in `reports/`.

The three mappings are CSVs under `data/sttm/`. Parquet outputs are under
`data/bronze_layer/`, `data/silver_layer/` and `data/gold_layer/`.
Every artifact carries the same UTC timestamp plus random run suffix.
Append-only JSONL events live in `audit_logs/`; a JSON state snapshot lives in
`data/traces/`. These are diagnostic checkpoints, not automatic resume support.

There are four application-level LLM requests on a successful live run: three mapping
requests and one reporter request. SDK retries are disabled. Credential checks,
failed attempts and new runs are additional requests. Bronze and Silver prompts
exclude business intent; Gold and the report include it. Responses remain
nondeterministic even at temperature zero, so human review is essential.

Offline review makes zero model requests and marks response events as
`review_fixture`. Approved mappings are SHA-256 hashed in the audit log. Set
`PIPELINE_OUTPUT_DIR` to an isolated output root when reproducing evidence; the
default remains this project directory. Input selection and fixtures are unchanged
by that setting. Relative output roots are resolved from the working directory.

`core/` owns configuration, state, audit, the shared LLM client, mapping validation
and reusable transformation helpers. `agents/` owns profiling, mapping generation,
layer execution and reporting. `cli.py` owns input copying and approval flow.

## Approved business policy

- Net revenue is the sum of reported `total_amount`, including negative returns.
- Never replace transaction prices with catalog `standard_price` or silently
    recalculate totals as `quantity * unit_price`. Flag discrepancies for review.
- Use the product catalog's category and product name where `product_id` matches.
    Preserve unmatched source values and count unmatched lookups in the audit log.
- Preserve unmatched store transactions in an unknown-region group.
- US date forms `MM/DD/YYYY` and `MM-DD-YYYY`, plus ISO `YYYY-MM-DD`, are supported.
    Month-first interpretation follows the supplied US data. Invalid non-null casts
    stop the run rather than silently becoming nulls.
- Deduplicate normalized business rows, excluding load metadata, before adding
    surrogate keys. This is exact-row deduplication, not transaction-ID deduplication.
- Monetary values and quantities cannot be imputed or rewritten by Silver rules.
    Other drop/fill operations require explicit mapping approval.

The first approved question is category net revenue. The mapping generator is
instructed to follow this policy; review all generated mappings, especially Gold
aggregates, before approving. Tests prove the supplied test mappings, not that
every possible future model response will answer every question correctly.

## Mapping contract

STTM files require exactly these columns, in order:

```text
source_schema,source_table,source_column,target_schema,target_table,target_column,transformation_type,transformation_logic
```

Use identifier-style names, not arbitrary expressions. Operation keywords are
case-sensitive. Table-level rows use `*` for their column fields.

| Layer | Type | Logic |
| --- | --- | --- |
| Bronze | `type_cast` | `datetime`, `float`, `int`, `str` |
| Bronze/Silver | `passthrough` | empty |
| Bronze | `metadata_inject` | empty; target `_load_timestamp` or `_source_file` |
| Silver | `normalize` | `strip`, `lowercase`, `uppercase`, `title case`, `date` |
| Silver | `map_values` | JSON object, e.g. `{"Prem":"Premium"}` |
| Silver | `lookup` | `products_bronze:product_id:category` |
| Silver | `fill_null` / `drop_null` | `mean`, `median`, `mode`, `0` / empty |
| Silver | `deduplicate` | `*`, or comma-separated column names |
| Gold | `join` | `join_left:sales_data_silver:stores_silver:store_id` |
| Gold | `group_by` | empty; source and target name the grouping column |
| Gold | `aggregate` | `SUM(total_amount) AS total_revenue`; also AVG/COUNT/MAX/MIN |

Use standard CSV quoting for JSON or comma-containing cells. Bronze must cover
every source column and both metadata fields. Bronze/Silver target tables follow
`<base>_bronze` and `<base>_silver`; Gold target names end `_gold`.
Silver retains approved target columns and adds `pk_<base>_silver_id` automatically.
Gold adds `pk_gold_id`. Reference lookup/join keys must be unique and non-null.
Gold joins must form a connected chain; right-side column collisions get
`__<right_table>` suffixes. Grouping retains null dimensions.

## Reporting and security

DuckDB permits one SELECT over one registered Gold table, with at most 500 result
rows and an allowlist of aggregate/scalar functions. External access and extension
loading are disabled. Joins belong in Gold, not reporter SQL. Unsafe SQL is rejected.
If otherwise allowed SQL fails to bind or execute, a limited preview is rendered
and explicitly labelled as not a verified business answer.

The visible executive summary is computed from executed results. The model's
unverified prose is retained only as `unverified_model_narrative` in the JSON
summary. Negative-value pie charts become bars. HTML text is escaped and Plotly is
embedded locally. Check JSON `status` to distinguish `query_executed` from `preview`.

In live mode, raw CSVs stay local, but column profiles, sample/distinct values, prior mappings,
business intent and up to 50 rows per Gold table are sent to GitHub Models. Use only
data approved for that external service. Audit logs do not record tokens, prompts
or raw provider errors. Normal generated artifacts can contain sensitive business
data and are ignored by Git. The explicitly retained offline sample under
`review/evidence/` is intended for code review and contains only the supplied
training data. Do not add private datasets or real-run credentials to that folder.

## Tests and CI

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q --cov=core --cov=agents --cov=cli --cov-branch --cov-report=term-missing --cov-fail-under=80
.\.venv\Scripts\python.exe -m ruff check core agents tests cli.py
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pip_audit --disable-pip --no-deps -r requirements.txt
```

Unit tests use in-memory frames and mock network, filesystem and clock boundaries.
Tests marked `integration` use isolated output directories, fake LLM responses and
scripted approval inputs. They exercise the supplied CSVs, independently reconcile
category and region totals, and preserve original file bytes. Actual subprocess
tests launch `cli.py --offline-review`, wait for each real approval prompt, assert
the process is still waiting and the next layer is absent, then supply one input.
They cover a successful run and rejection at each of the three gates.
No API key is required. Scripted inputs are test-only, not a CLI bypass.

To retain a fresh evidence set, add `--review-evidence review/evidence-new` to the
pytest command. Choose a new directory each time; existing evidence is never
overwritten. Without that option, test outputs remain in temporary directories.
Dependency auditing uses a vulnerability advisory service, not a model API.

The workflow in `.github/workflows/tests.yml` is a template for making this folder
the repository root. It runs lint, tests, coverage gates, dependency consistency and
vulnerability checks on Windows and Ubuntu with Python 3.12, then uploads offline
evidence and coverage results as CI artifacts. Tests and evidence need no API keys.
No Git repository or hosted CI run has been created here. If this folder is placed
inside a larger repository, move the workflow to its root `.github/workflows/` and
set the working directory and artifact paths to this project before submitting.

## Current limits and release notes

- Pre-key review update: explicit offline fixtures, retained datasets and manifests,
  genuine blocking CLI tests, mapping approval hashes, lint and dependency audit gates.
- Initial release: CLI, four-request model flow, three approval gates, all three
    layers, restricted reporting, audit/state artifacts and regression tests.
- This is a local batch training tool, not a production orchestrator or sandbox
    for arbitrary code. There is no web app, multi-provider routing or deployment.
- No automatic resume, incremental ingestion, or reuse of layers across runs.
- No arbitrary filters, derived month columns, currency conversion, or custom
    formulas in mappings. Such questions need explicitly implemented operations.
- Values use pandas numeric types rather than fixed-point accounting decimals.
    Reconciliation tests use tolerances; reports format numerical summaries to two decimals.
- Data-type and join errors fail closed. Fix the rule/data and start a new run;
    existing run artifacts are retained for diagnosis.
