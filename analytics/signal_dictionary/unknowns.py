from collections import Counter, defaultdict
from typing import Any

from domain.schemas.parsing import ParseResult


def accumulate_unknown_signals(parse_results: list[ParseResult]) -> list[dict[str, Any]]:
    occurrences: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    examples: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for result in parse_results:
        for table in result.tables:
            for row in table.rows:
                for column in table.unknown_columns:
                    value = row.get(column)
                    key = (column, result.file_family.value)
                    occurrences[key][str(value)] += 1
                    if len(examples[key]) < 5:
                        examples[key].append({"value": value, "parser": result.parser_name})
    reports = []
    for (field_name, family), distribution in occurrences.items():
        reports.append(
            {
                "unknown_field_name": field_name,
                "source_file_family": family,
                "occurrence_count": sum(distribution.values()),
                "value_distribution": dict(distribution.most_common(20)),
                "candidate_semantic_class": candidate_semantic_class(field_name),
                "confidence": 0.25,
                "examples": examples[(field_name, family)],
            }
        )
    return reports


def candidate_semantic_class(field_name: str) -> str:
    upper = field_name.upper()
    if upper.startswith("SO"):
        return "oxygen_or_gas_candidate"
    if upper.startswith("ST"):
        return "temperature_or_status_candidate"
    if upper.startswith("SP"):
        return "pressure_candidate"
    if upper.startswith("SF"):
        return "flow_or_filter_candidate"
    if upper.startswith("BI"):
        return "binary_input_candidate"
    return "unknown"

