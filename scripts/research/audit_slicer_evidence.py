"""Read-only comparison of supplied screenshots, a log ZIP and local model files.

Only requested output is written. Large sensor entries are never extracted;
timings use the production parser/admission rules, across daily file boundaries.
Screenshot candidates do not automatically become confirmed database links.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import zipfile
from pathlib import Path
from statistics import median

from analytics.prediction.layer_engine import LayerGeometrySeries, physics_scan_seconds_by_layer
from analytics.prediction.magics_reader import read_plate
from analytics.prediction.timing_validation import (
    calibration_cycles_ms, calibration_timing_payloads, timing_components_ms,
)
from core.versioning.constants import ANALYSIS_VERSION
from core.versioning.provenance import build_provenance
from parsers.base.base import ParserContext
from parsers.formats.time_log import TimeLogParser


def sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def timing_summary(events: list) -> dict:
    payloads = calibration_timing_payloads(events)
    rows = timing_components_ms(payloads)
    cycles = calibration_cycles_ms(payloads)
    burn = sum(row[0] for row in rows.values()) / 3_600_000 if rows else None
    pour = sum(row[1] for row in rows.values()) / 3_600_000 if rows else None
    return {
        'status': 'measured_partial_components' if rows else 'unavailable',
        'admitted_layers': len(rows), 'first_layer': min(rows) if rows else None,
        'last_layer': max(rows) if rows else None,
        'missing_within_observed_range': sorted(set(range(1, max(rows) + 1)) - rows.keys()) if rows else [],
        'observed_burn_hours': burn, 'observed_pour_hours': pour,
        'observed_components_hours': burn + pour if rows else None,
        'median_pour_ms': median(row[1] for row in rows.values()) if rows else None,
        'normal_whole_print_hours': None,
        'reason': 'Screenshot slice indexing and an independent whole normal-cycle target are not established.',
        'complete_raw_cycle_rows': len(cycles),
    }


def compute_components(job: dict, evidence: dict) -> dict:
    """Explicit screenshot quantities; no synthetic instance placement or fit."""
    from analytics.prediction.plate_estimator import estimate_plate

    hatch = evidence['hatch_observations'][job['hatch_observation']]
    root = Path(job['model_root'])
    params = {
        'layer_thickness_mm': job['layer_thickness_mm'],
        'hatch_distance_mm': hatch['hatch_distance_mm'],
        'hatch_speed_mm_s': hatch['hatch_speed_mm_s'],
        'hatch_angle_deg': hatch['hatch_angle_deg'], 'contours_enabled': job['contours_enabled'],
        'jump_speed_mm_s': hatch['jump_speed_mm_s'], 'contour_speed_mm_s': 510.0,
        'support_speed_mm_s': job['support']['speed_mm_s'], 'laser_count': 1,
        'time_correction_factor': 1.0, 'recoat_time_ms': 9250.0, 'build_origin_z_mm': 0.0,
    }
    result = {'job_id': job['id'], 'scope': 'post_hoc_component_sensitivity_not_full_prediction',
              'params': params, 'groups': {}, 'limitations': [
        'Instance transforms are not reconstructed.',
        'Inter-instance travel and per-layer angle rotation are not reconstructed.',
        'Open supports are single tracks, without their internal laser-off travel.',
        'The support skip rule is not applied by this estimator.',
        'Per-model recipe equality is not established by one selected-model screenshot.',
        'The measured normal full-cycle target has not been established.',
        'Z=0 is an explicit experiment hypothesis, not a decoded project datum.',
        '9250 ms recoat is a post-hoc log-derived diagnostic value, not a before-print prediction.',
    ]}
    for group in job['component_groups']:
        part = root / group['part']
        supports = [(name, root / name) for name in group['supports']]
        paths = [part, *(p for _, p in supports)]
        checksums = {p.name: sha256(p) for p in paths}
        estimate = estimate_plate([(part.name, part)], supports, params, 'unconfirmed')
        if checksums != {p.name: sha256(p) for p in paths}:
            raise ValueError('The source models changed during calculation.')
        result['groups'][part.stem] = {
            'quantity_on_screenshot': group['copies'], 'input_checksums': checksums,
            'scan_hours_per_copy': estimate.scan_hours, 'layer_count': estimate.layer_count,
            'geometry_totals': estimate.geometry_totals,
            'series': estimate.geometry_series.to_snapshot(), 'warnings': estimate.warnings,
        }
        print(f"Calculated component: {part.name}", flush=True)
    return result


def component_comparison(experiment: dict, events: list) -> dict:
    """Conditional multiplicity sensitivity, aligned only to measured layer rows.

    Do not fill missing measurements, fit a factor, or declare an as-run recipe.
    Bounds, rotations, instance travel, skip parity and channel timing remain
    limitations even if the sum happens to be close to a measured subtotal.
    """
    import numpy as np

    rows = timing_components_ms(calibration_timing_payloads(events))
    if not rows:
        return {'available': False}
    layers = sorted(rows)
    params = experiment['params']
    zs = (np.asarray(layers, dtype=float) - 0.5) * params['layer_thickness_mm']
    measured = np.asarray([rows[n][0] / 1000 for n in layers])
    pour = np.asarray([rows[n][1] / 1000 for n in layers])
    copies = np.zeros(len(layers))
    single = np.zeros(len(layers))
    reference = np.zeros(len(layers))
    for name, group in experiment['groups'].items():
        series = LayerGeometrySeries.from_snapshot(group['series'])
        # Use the existing physics function on this exact, ordered projection.
        # A flat series with one point per observed row must not re-fill gaps.
        seconds = np.asarray(physics_scan_seconds_by_layer(
            series, params['layer_thickness_mm'], params, 'unconfirmed', 1, heights=zs,
        ))
        seconds[(zs < series.z_min) | (zs > series.z_max)] = 0.0
        if name == 'Rounded Box':
            reference += seconds
        else:
            copies += seconds * group['quantity_on_screenshot']
            single += seconds
    # Conditional ideal equal sharing, not inference from channel capacity.
    conditional = copies / 2
    baseline = single / 2
    estimated_pour = np.full(len(layers), params['recoat_time_ms'] / 1000)
    total = conditional + estimated_pour
    result = {
        'scope': 'post_hoc_partial_component_diagnostic_not_whole_print_accuracy',
        'layers': len(layers), 'conditional_laser_policy': 'ideal_balanced_two_channels',
        'reference_scan_hours_if_enabled_on_one_channel': float(reference.sum()) / 3600,
        'source_series_precision': 'compact rounded geometry; approximately 90 height samples per body',
        'single_copy_components_hours': float((baseline + estimated_pour).sum()) / 3600,
        'screenshot_quantity_components_hours': float(total.sum()) / 3600,
        'observed_components_hours': float((measured + pour).sum()) / 3600,
        'partial_component_sum_error_pct': float((total.sum() / (measured + pour).sum() - 1) * 100),
        'burn_wape_pct': float(np.abs(conditional - measured).sum() / measured.sum() * 100),
        'odd_layer_median_burn_seconds': float(np.median(measured[np.asarray(layers) % 2 == 1])),
        'even_layer_median_burn_seconds': float(np.median(measured[np.asarray(layers) % 2 == 0])),
        'limitations': experiment['limitations'],
    }
    return result


def audit(evidence_path: Path, archive_path: Path, component_path: Path | None,
          compute_job_id: str | None = None) -> dict:
    evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
    document = Path(evidence['source']['path'])
    if sha256(document) != evidence['source']['sha256']:
        raise ValueError('The screenshot document changed; re-verify the transcription.')
    parser = TimeLogParser()
    events_by_name, time_files = {}, []
    with zipfile.ZipFile(archive_path) as archive, tempfile.TemporaryDirectory(prefix='pc-time-evidence-') as scratch:
        entries = archive.infolist()
        for entry in entries:
            if entry.is_dir() or not entry.filename.endswith('_time.log'):
                continue
            if entry.file_size > 64 * 1024 * 1024:
                raise ValueError(f'Oversized timing entry: {entry.filename}')
            name = Path(entry.filename).name
            if name in events_by_name:
                raise ValueError(f'Ambiguous archive path for {name}')
            blob = archive.read(entry)  # CRC checked; no unchecked path extraction.
            local = Path(scratch) / name
            local.write_bytes(blob)
            parsed = parser.parse(local, ParserContext())
            events_by_name[name] = parsed.events
            time_files.append({'file': name, 'sha256': hashlib.sha256(blob).hexdigest(),
                               'bytes': len(blob), 'parser_metadata': parsed.metadata,
                               **timing_summary(parsed.events)})
    jobs = []
    for job in evidence['jobs']:
        names = job['candidate_time_logs']
        events = [event for name in names for event in events_by_name.get(name, [])]
        jobs.append({
            'id': job['id'], 'project': job['project'], 'source_images': job['images'],
            'project_slice_count': job['project_slices'],
            'selected_model_slice_count': job['selected_model_slices'],
            'candidate_logs': names, 'missing_log_files': [n for n in names if n not in events_by_name],
            'candidate_not_confirmed_link': True, **timing_summary(events),
        })
    geometry = []
    for job in evidence['jobs']:
        for name in job.get('model_files', []):
            path = Path(job['model_root']) / name
            row = {'job_id': job['id'], 'file': name, 'sha256': sha256(path)}
            if path.suffix == '.magics':
                plate = read_plate(path)
                row.update(decoded_mesh_bodies=len(plate.parts), geometry_quality=plate.geometry_quality)
            else:
                import trimesh

                mesh = trimesh.load_mesh(path, process=False)
                row.update(triangles=len(mesh.faces), bounds_mm=mesh.bounds.tolist())
            geometry.append(row)
    report = {
        'analysis_version': ANALYSIS_VERSION, 'parser_version': parser.version,
        'evidence_sha256': sha256(evidence_path), 'document_sha256': evidence['source']['sha256'],
        'archive_sha256': sha256(archive_path), 'archive_entries': len(entries),
        'archive_uncompressed_bytes': sum(e.file_size for e in entries),
        'archive_integrity_scope': 'CRC validated for time logs only; other members were not fully read',
        'timing_files': time_files, 'screenshot_jobs': jobs, 'geometry': geometry,
        'database_modified': False, 'source_files_modified': False,
    }
    experiment = None
    if compute_job_id:
        job = next(j for j in evidence['jobs'] if j['id'] == compute_job_id)
        experiment = compute_components(job, evidence)
    elif component_path:
        experiment = json.loads(component_path.read_text(encoding='utf-8'))
        job = next(j for j in evidence['jobs'] if j['id'] == '2026-05-27')
        report['component_evidence_sha256'] = sha256(component_path)
    if experiment:
        events = [event for n in job['candidate_time_logs'] for event in events_by_name[n]]
        report['rs320_component_diagnostic'] = component_comparison(experiment, events)
        report['component_experiment'] = experiment
    report['provenance'] = build_provenance(
        'slicer_evidence_audit', generated_by='local-read-only-research',
        inputs={k:report[k] for k in ('document_sha256', 'archive_sha256', 'evidence_sha256', 'geometry')},
        config=experiment['params'] if experiment else None,
        parser_versions={parser.name: parser.version},
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', required=True, type=Path)
    parser.add_argument('--logs', required=True, type=Path)
    components = parser.add_mutually_exclusive_group()
    components.add_argument('--components', type=Path)
    components.add_argument('--compute-components-for', choices=['2026-05-27'])
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    report = audit(args.evidence, args.logs, args.components, args.compute_components_for)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({
        'time_files': len(report['timing_files']), 'screenshot_jobs': len(report['screenshot_jobs']),
        'archive_sha256': report['archive_sha256'],
        'component_diagnostic': report.get('rs320_component_diagnostic'),
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
