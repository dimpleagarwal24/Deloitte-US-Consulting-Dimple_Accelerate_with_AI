from dataclasses import dataclass, field


@dataclass
class PipelineState:
    run_id: str
    business_intent: str
    input_files: list[str] = field(default_factory=list)
    profile_path: str | None = None
    bronze_sttm_path: str | None = None
    bronze_paths: list[str] = field(default_factory=list)
    silver_sttm_path: str | None = None
    silver_paths: list[str] = field(default_factory=list)
    gold_sttm_path: str | None = None
    gold_paths: list[str] = field(default_factory=list)
    report_path: str | None = None
    status: str = "created"
    execution_mode: str = "live"