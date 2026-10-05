"""Research-only replay of two assembly-checked LaserStudio rules.

This does not execute the PE or install its rules into the production estimator.
The real-STL probe isolates body-angle sensitivity at selected measured heights;
it is not a whole-print prediction or an as-run recipe reconstruction.
"""
from __future__ import annotations

import argparse
import json
import math
import socket
from pathlib import Path
from time import perf_counter

from scripts.research.audit_slicer_evidence import sha256, timing_summary

BINARY_SHA256 = '00d1f021120b0c16f6570515a2584b2bc3b45683abf6c8e6c62bdeff0b4c29ef'
RULE_VERSION = 'laserstudio-00d1f021-angle-cones-v1'


def _int32(value: int) -> int:
    if type(value) is not int or not -(2**31) <= value < 2**31:
        raise ValueError('Replay requires signed int32 values, not booleans or floats.')
    return value


def replay_hatch_angle(*, slice_index: int, mode: int, angle: int, tabu_angle: int) -> int:
    """0x140095360; index is a slicer index, not an inferred log layer number.

    Unknown modes and arithmetic overflow are deliberately outside this replay.
    The original has an existing-state branch for other modes; it is not a
    stateless fallback. Signed division truncates toward zero, unlike Python //.
    """
    for value in (slice_index, mode, angle, tabu_angle):
        _int32(value)
    if slice_index < 0 or mode not in (0, 1, 2):
        raise ValueError('Only nonnegative indices and modes 0, 1, 2 are supported.')
    if mode == 1:
        return 45 if slice_index % 2 == 0 else -45
    if mode == 2:
        return angle
    value = angle
    if slice_index:
        increment = _int32(33 * slice_index)
        numerator = _int32(_int32(angle + increment) + 45)
        quotient = abs(numerator) // 90 * (-1 if numerator < 0 else 1)
        value = _int32(_int32(increment - _int32(90 * quotient)) + angle)
    tabu_min, tabu_max = _int32(tabu_angle - 5), _int32(tabu_angle + 5)
    if tabu_min < value < tabu_max:
        value = _int32(value + 10)
    if -4 <= value <= 4:
        value = _int32(value + (10 if tabu_angle <= 0 else -10))
    return value


def replay_cone_active(*, slice_index: int, skip: int) -> bool:
    """0x1400893D0 / model+0x60: ConesStructure, NOT a support rule."""
    _int32(slice_index)
    _int32(skip)
    if slice_index < 0 or skip < 0:
        raise ValueError('Only nonnegative indices and skip counts are supported.')
    return slice_index % _int32(skip + 1) == 0


def probe(*, evidence_path: Path, audit_path: Path, rules_path: Path,
          binary_path: Path, logs_root: Path, layers: list[int],
          build_origin_z_mm: float, tabu_angle: int) -> dict:
    """No DB, NAS, fit, synthetic placement, support skipping or missing rows."""
    import trimesh

    from analytics.prediction.layer_engine import (
        GEOMETRY_FEATURES, LayerGeometrySeries, _hatch_level, _make_hatcher,
        physics_scan_seconds_by_layer,
    )
    from analytics.prediction.timing_validation import calibration_timing_payloads, timing_components_ms
    from core.versioning.provenance import build_provenance
    from parsers.base.base import ParserContext
    from parsers.formats.time_log import TimeLogParser

    started = perf_counter()
    evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
    audit = json.loads(audit_path.read_text(encoding='utf-8'))
    rules = json.loads(rules_path.read_text(encoding='utf-8'))
    if sha256(binary_path) != BINARY_SHA256 or rules['source']['sha256'] != BINARY_SHA256:
        raise ValueError('The recovered rules do not describe this executable.')
    document_path = Path(evidence['source']['path'])
    if sha256(document_path) != evidence['source']['sha256']:
        raise ValueError('The screenshot document must match the reviewed source.')
    if not layers or layers != sorted(set(layers)) or any(type(n) is not int or n < 1 for n in layers):
        raise ValueError('Supply ascending, unique, positive measured layer numbers.')
    if not math.isfinite(build_origin_z_mm):
        raise ValueError('The experimental datum must be finite.')
    job = next(j for j in evidence['jobs'] if j['id'] == '2026-05-27')
    hatch = evidence['hatch_observations'][job['hatch_observation']]
    inputs = {str(p): sha256(p) for p in (
        evidence_path, audit_path, rules_path, binary_path, document_path, Path(__file__),
        Path(_hatch_level.__code__.co_filename), Path(TimeLogParser.parse.__code__.co_filename),
        Path(timing_components_ms.__code__.co_filename),
    )}
    events, parser = [], TimeLogParser()
    audited_logs = {row['file']: row['sha256'] for row in audit['timing_files']}
    for name in job['candidate_time_logs']:
        path = logs_root / name
        inputs[str(path)] = sha256(path)
        if inputs[str(path)] != audited_logs[name]:
            raise ValueError(f'The local log is not the previously audited archive member: {name}')
        events.extend(parser.parse(path, ParserContext()).events)
    measured = timing_components_ms(calibration_timing_payloads(events))
    if not set(layers).issubset(measured):
        raise ValueError('A requested layer has no admitted measurement; it will not be filled.')
    meshes = {}
    geometry_hashes = {row['file']: row['sha256'] for row in audit['geometry'] if row['job_id'] == job['id']}
    assignments = job['observed_laser_assignments']
    for group in job['component_groups']:
        key = group['screenshot_group']
        if key == 'reference':
            continue  # Unconfirmed activity; no silent contribution.
        assigned = [n for channel in assignments.values() for n in channel[key]]
        if len(set(assigned)) != group['copies'] or len(assigned) != group['copies']:
            raise ValueError('Screenshot quantities and assignment rows disagree.')
        path = Path(job['model_root']) / group['part']
        inputs[str(path)] = sha256(path)
        if inputs[str(path)] != geometry_hashes[path.name]:
            raise ValueError(f'The model changed since the source audit: {path.name}')
        meshes[key] = trimesh.load_mesh(path, process=False)
    params = {'hatch_speed_mm_s': hatch['hatch_speed_mm_s'], 'jump_speed_mm_s': hatch['jump_speed_mm_s']}
    config = {
        'job_id': job['id'], 'layers': layers, 'layer_thickness_mm': job['layer_thickness_mm'],
        'build_origin_z_mm_hypothesis': build_origin_z_mm, 'tabu_angle_hypothesis': tabu_angle,
        'hatch_distance_mm': hatch['hatch_distance_mm'], 'base_angle_deg': hatch['hatch_angle_deg'],
        'contours_enabled': job['contours_enabled'], 'body_params_hypothesis': params,
        'laser_assignments': assignments, 'index_hypotheses': ['k=log_layer-1', 'k=log_layer'],
        'core_hatcher_offsets_mm': {'volume_offset': 0.08, 'spot_compensation': 0.06},
        'jump_delay_ms_hypothesis': 0.0,
    }
    samples = []
    for n in layers:
        z = build_origin_z_mm + (n - 0.5) * job['layer_thickness_mm']
        scenarios = {}
        for label, angle in (
            ('fixed', hatch['hatch_angle_deg']),
            ('variable_k_n_minus_1', replay_hatch_angle(slice_index=n-1, mode=0,
                angle=int(hatch['hatch_angle_deg']), tabu_angle=tabu_angle)),
            ('variable_k_n', replay_hatch_angle(slice_index=n, mode=0,
                angle=int(hatch['hatch_angle_deg']), tabu_angle=tabu_angle)),
        ):
            components = {}
            for key, mesh in meshes.items():
                hatcher = _make_hatcher(hatch['hatch_distance_mm'],
                    contours_enabled=job['contours_enabled'], hatch_angle_deg=angle)
                metrics = _hatch_level([mesh], z, hatcher)[:len(GEOMETRY_FEATURES)]
                series = LayerGeometrySeries(zs=[z], z_min=z, z_max=z,
                    **{name: [value] for name, value in zip(GEOMETRY_FEATURES, metrics, strict=True)})
                seconds = physics_scan_seconds_by_layer(series, job['layer_thickness_mm'],
                    params, 'unconfirmed', 1, heights=[z])[0]
                components[key] = {'seconds_per_copy': seconds,
                    'geometry': dict(zip(GEOMETRY_FEATURES, metrics, strict=True))}
            channels = {laser: sum(components[key]['seconds_per_copy'] * len(rows[key])
                for key in meshes) for laser, rows in assignments.items()}
            scenarios[label] = {'angle_deg': angle, 'components': components,
                'channel_body_seconds': channels, 'conditional_concurrent_body_seconds': max(channels.values())}
        samples.append({'log_layer': n, 'height_mm_hypothesis': z,
            'observed_burn_seconds': measured[n][0] / 1000, 'scenarios': scenarios})
    totals = {key: sum(row['scenarios'][key]['conditional_concurrent_body_seconds'] for row in samples)
        for key in samples[0]['scenarios']}
    fixed = totals['fixed']
    relative = {key: (value / fixed - 1) * 100 if fixed else None for key, value in totals.items()}
    if any(sha256(Path(path)) != expected for path, expected in inputs.items()):
        raise ValueError('A source changed during the probe; no result is publishable.')
    return {
        'schema_version': 1, 'scope': 'post_hoc_selected_body_angle_sensitivity_not_accuracy',
        'source': 'calculated', 'binary_execution_performed': False,
        'input_sha256': inputs, 'config': config, 'measurement_summary': timing_summary(events),
        'samples': samples, 'selected_body_totals_seconds': totals,
        'selected_body_angle_change_pct': relative, 'elapsed_seconds': perf_counter() - started,
        'limitations': [
            'Rules describe the supplied LaserStudio hash with embedded UI string 2.0.6.35; transfer to screenshot MasterSLM 2.0.6.033 is unverified.',
            'The two slice-index mappings and datum are explicit hypotheses, not recovered as-run values.',
            'Selected model settings are conditionally applied to both body types.',
            'Supports, cones/thermal bridges, reference body, skin and stripe strategies are excluded.',
            'Individual STL orientations are retained; screenshot instance transforms and inter-instance jumps are unknown.',
            'The maximum of assigned channel loads assumes concurrent lasers; assignment alone does not prove concurrency.',
            f'This is a deterministic {len(layers)}-height probe, not a random sample or a whole-print estimate.',
            'Native runtime, delay units, as-run phase recipe and independent normal full-cycle target remain unknown.',
        ],
        'provenance': build_provenance('laserstudio_body_angle_probe', inputs=inputs, config=config,
            parser_versions={parser.name: parser.version}, model_versions={'rule_replay': RULE_VERSION},
            generated_by=f'local-read-only-research@{socket.gethostname()}'),
    }


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    for name in ('evidence', 'audit', 'rules', 'binary', 'logs-root'):
        cli.add_argument(f'--{name}', required=True, type=Path)
    cli.add_argument('--layers', required=True, type=int, nargs='+')
    cli.add_argument('--build-origin-z-mm', required=True, type=float)
    cli.add_argument('--tabu-angle', required=True, type=int)
    args = cli.parse_args()
    result = probe(evidence_path=args.evidence, audit_path=args.audit, rules_path=args.rules,
        binary_path=args.binary, logs_root=args.logs_root, layers=args.layers,
        build_origin_z_mm=args.build_origin_z_mm, tabu_angle=args.tabu_angle)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
