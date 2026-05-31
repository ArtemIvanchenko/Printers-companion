from collections import defaultdict

from domain.schemas.parsing import CanonicalEventDraft


def deduplicate_events(events: list[CanonicalEventDraft], time_bucket_seconds: int = 2) -> tuple[list[CanonicalEventDraft], list[dict[str, object]]]:
    buckets: dict[tuple[object, ...], list[CanonicalEventDraft]] = defaultdict(list)
    for event in events:
        if event.ts is None:
            key = ("no_ts", event.event_type, event.layer, event.source.raw_excerpt)
        else:
            bucket = int(event.ts.timestamp() // time_bucket_seconds)
            key = (bucket, event.event_type, event.layer, event.subsystem)
        buckets[key].append(event)

    merged: list[CanonicalEventDraft] = []
    diagnostics: list[dict[str, object]] = []
    for group in buckets.values():
        canonical = group[0]
        if len(group) > 1:
            provenance = [
                {
                    "source_file_id": item.source.source_file_id,
                    "source_line": item.source.source_line,
                    "source_offset": item.source.source_offset,
                    "raw_excerpt": item.source.raw_excerpt,
                }
                for item in group
            ]
            canonical.payload["deduplicated_provenance"] = provenance
            canonical.confidence = max(item.confidence for item in group)
            diagnostics.append(
                {
                    "code": "deduplicated_semantic_equivalent_events",
                    "event_type": canonical.event_type,
                    "count": len(group),
                }
            )
        merged.append(canonical)
    return sorted(merged, key=lambda event: event.ts.timestamp() if event.ts else (event.source.source_line or 0)), diagnostics

