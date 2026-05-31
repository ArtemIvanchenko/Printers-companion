from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class EvidenceLink(BaseModel):
    evidence_type: str
    evidence_id: str | None = None
    source_file_id: str | None = None
    source_line: int | None = None
    source_offset: int | None = None
    raw_excerpt: str | None = None


class ReportSection(BaseModel):
    title: str
    content: dict[str, Any] = Field(default_factory=dict)
    evidence_links: list[EvidenceLink] = Field(default_factory=list)


class SessionReport(BaseModel):
    report_id: str
    session_id: str
    generated_at: datetime
    generated_by: str = "system"
    analysis_version: str
    profile_version: str | None = None
    parser_versions: dict[str, str] = Field(default_factory=dict)
    input_file_hashes: dict[str, str] = Field(default_factory=dict)
    signal_dictionary_version: str | None = None
    rule_pack_version: str | None = None
    causal_model_version: str | None = None
    llm_prompt_version: str | None = None
    llm_model_version: str | None = None
    sections: list[ReportSection] = Field(default_factory=list)

