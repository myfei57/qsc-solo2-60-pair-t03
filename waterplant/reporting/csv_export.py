"""CSV rendering for operator exports."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from waterplant.audit import Auditor
    from waterplant.console.telemetry import Telemetry

QUOTE_TRIGGERS = (",", '"', "\n")


def _escape(value: object) -> str:
    text = "" if value is None else str(value)
    if any(trigger in text for trigger in QUOTE_TRIGGERS):
        return '"' + text.replace('"', '""') + '"'
    return text


def to_csv(columns: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """Render a header row and body rows as comma separated text."""

    lines = [",".join(_escape(column) for column in columns)]
    for row in rows:
        lines.append(",".join(_escape(value) for value in row))
    return "\n".join(lines) + "\n"


def audit_export(auditor: Auditor) -> tuple[list[str], list[list[str]]]:
    """Columns and rows for the dosing audit trail."""

    columns = ["time", "kind", "detail", "id"]
    rows = [[entry.time, entry.kind, entry.detail, entry.id] for entry in auditor.entries()]
    return columns, rows


def telemetry_export(telemetry: Telemetry) -> tuple[list[str], list[list[object]]]:
    """Columns and rows for the telemetry counters."""

    return ["metric", "value"], [[key, value] for key, value in telemetry.as_dict().items()]


def chlorination_export(auditor: Auditor) -> tuple[list[str], list[list[object]]]:
    """Columns and rows for the feedforward/feedback chlorine audit trail."""

    columns = [
        "time",
        "kind",
        "round_id",
        "sequence",
        "phase",
        "mg_l",
        "kg_h",
        "target_mg_l",
        "ammonia_n_mg_l",
        "contact_time_minutes",
        "contact_basis",
        "flow_m3h",
        "residual_mg_l",
        "error_mg_l",
        "reading_id",
        "request_id",
        "id",
    ]
    rows: list[list[object]] = []
    for entry in auditor.entries():
        if not entry.kind.startswith("chlorine"):
            continue
        metadata = entry.metadata or {}
        rows.append(
            [
                entry.time,
                entry.kind,
                metadata.get("round_id", ""),
                metadata.get("sequence", ""),
                metadata.get("phase", "feedback" if entry.kind.endswith("feedback") else "event"),
                metadata.get("mg_l", entry.detail if entry.kind.endswith(("feedforward", "feedback")) else ""),
                metadata.get("kg_h", ""),
                metadata.get("target_mg_l", ""),
                metadata.get("ammonia_n_mg_l", ""),
                metadata.get("contact_time_minutes", ""),
                metadata.get("contact_basis", ""),
                metadata.get("flow_m3h", ""),
                metadata.get("residual_mg_l", ""),
                metadata.get("error_mg_l", ""),
                metadata.get("reading_id", ""),
                metadata.get("request_id", ""),
                entry.id,
            ]
        )
    return columns, rows
