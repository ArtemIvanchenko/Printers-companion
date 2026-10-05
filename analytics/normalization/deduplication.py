import json

from domain.schemas.parsing import CanonicalEventDraft


def deduplicate_events(events: list[CanonicalEventDraft], time_bucket_seconds: int = 2) -> tuple[list[CanonicalEventDraft], list[dict[str, object]]]:
    buckets: dict[tuple[object, ...], tuple[float | int, list[CanonicalEventDraft]]] = {}
    for event in events:
        # Distinct measured payloads/retry attempts are never duplicates just
        # because they share a clock bucket (or have no absolute timestamp).
        payload_key = json.dumps(event.payload, sort_keys=True, default=str)
        if event.ts is None:
            sort_key = event.source.source_line or 0
            key = ("no_ts", event.event_type, event.layer, event.source.raw_excerpt, payload_key)
        else:
            sort_key = event.ts.timestamp()
            bucket = int(sort_key // time_bucket_seconds)
            key = (bucket, event.event_type, event.layer, event.subsystem, payload_key)
        # Keep the first fact's precise time, not the bucket boundary or the
        # earliest duplicate. It remains the canonical event after merging.
        buckets.setdefault(key, (sort_key, []))[1].append(event)

    merged: list[tuple[float | int, CanonicalEventDraft]] = []
    diagnostics: list[dict[str, object]] = []
    for sort_key, group in buckets.values():
        # Never annotate the original parser fact. Timing validation and other
        # projections may still need its unmodified payload/source evidence.
        canonical = group[0].model_copy(deep=True)
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
        merged.append((sort_key, canonical))
    merged.sort(key=lambda item: item[0])
    return [event for _, event in merged], diagnostics
