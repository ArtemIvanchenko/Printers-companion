"""Offline, no-fit phase diagnostic on every admitted RS320 log layer.

This calculates bodies, ordinary support tracks and the separate extra STL
without co-hatching them. Activation, phase recipe, native path order and laser
concurrency remain declared hypotheses, never production calibration inputs.
"""
from __future__ import annotations

import argparse
import json
import math
import socket
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import perf_counter

import numpy as np

from analytics.prediction.layer_engine import (
    GEOMETRY_FEATURES, LayerGeometrySeries, _hatch_level, _make_hatcher,
    physics_scan_seconds_by_layer, section_track_metrics_by_height,
)
from analytics.prediction.timing_validation import calibration_timing_payloads, timing_components_ms
from core.versioning.provenance import build_provenance
from parsers.base.base import ParserContext
from parsers.formats.time_log import TimeLogParser
from scripts.research.audit_slicer_evidence import component_comparison, sha256, timing_summary
from scripts.research.probe_laserstudio_rules import BINARY_SHA256, RULE_VERSION, replay_hatch_angle


def _seconds(features, heights, thickness, params):
    series = LayerGeometrySeries(zs=heights.tolist(), z_min=float(heights[0]),
        z_max=float(heights[-1]), **{name: features[:, i].tolist()
            for i, name in enumerate(GEOMETRY_FEATURES)})
    return np.asarray(physics_scan_seconds_by_layer(series, thickness, params,
        'unconfirmed', 1, heights=heights))


def _body(path, heights, layers, hatch, contours, tabu):
    import trimesh

    started = perf_counter()
    mesh = trimesh.load_mesh(path, process=False)
    result = {}
    for scenario, indices in (('fixed', None), ('variable_k_n_minus_1', layers - 1),
                              ('variable_k_n', layers)):
        features = []
        for i, z in enumerate(heights):
            angle = hatch['hatch_angle_deg'] if indices is None else replay_hatch_angle(
                slice_index=int(indices[i]), mode=0, angle=int(hatch['hatch_angle_deg']), tabu_angle=tabu)
            hatcher = _make_hatcher(hatch['hatch_distance_mm'], contours_enabled=contours,
                                   hatch_angle_deg=angle)
            features.append(_hatch_level([mesh], float(z), hatcher)[:len(GEOMETRY_FEATURES)])
        result[scenario] = np.asarray(features)
    return {'kind': 'body_hatch', 'features': result, 'elapsed_seconds': perf_counter() - started}


def _tracks(path, heights):
    import trimesh

    started = perf_counter()
    mesh = trimesh.load_mesh(path, process=False)
    result = section_track_metrics_by_height(mesh, heights)
    features = np.zeros((len(heights), len(GEOMETRY_FEATURES)))
    features[:, 2] = [r['jump_mm'] for r in result]
    features[:, 3] = [r['n_jumps'] for r in result]
    features[:, 4] = [r['mark_mm'] for r in result]
    return {'kind': 'section_tracks', 'features': features,
            'track_counts': [r['n_tracks'] for r in result],
            'elapsed_seconds': perf_counter() - started}


def error_summary(predicted, observed):
    """Positive measured rows only; retain each error before aggregation."""
    errors = predicted - observed
    return {'layers': len(observed), 'predicted_burn_hours': float(predicted.sum()) / 3600,
        'observed_burn_hours': float(observed.sum()) / 3600,
        'burn_sum_error_pct': float(errors.sum() / observed.sum() * 100),
        'burn_wape_pct': float(np.abs(errors).sum() / observed.sum() * 100),
        'median_absolute_error_seconds': float(np.median(np.abs(errors))),
        'p90_absolute_error_seconds': float(np.quantile(np.abs(errors), .9))}


def probe(*, evidence_path: Path, audit_path: Path, rules_path: Path, binary_path: Path,
          logs_root: Path, origin: float, tabu: int, allow_offline_transcription: bool,
          progress=lambda message: None) -> dict:
    started = perf_counter()
    if not math.isfinite(origin):
        raise ValueError('Datum hypothesis must be finite.')
    evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
    audit = json.loads(audit_path.read_text(encoding='utf-8'))
    rules = json.loads(rules_path.read_text(encoding='utf-8'))
    inputs = {str(p): sha256(p) for p in (evidence_path, audit_path, rules_path, binary_path,
        Path(__file__), Path(_hatch_level.__code__.co_filename),
        Path(TimeLogParser.parse.__code__.co_filename), Path(timing_components_ms.__code__.co_filename),
        Path(component_comparison.__code__.co_filename), Path(replay_hatch_angle.__code__.co_filename))}
    if inputs[str(binary_path)] != BINARY_SHA256 or rules['source']['sha256'] != BINARY_SHA256:
        raise ValueError('Wrong executable for recovered angle rules.')
    if audit['document_sha256'] != evidence['source']['sha256']:
        raise ValueError('Screenshot transcription and prior audited document disagree.')
    document = Path(evidence['source']['path'])
    document_status = 'currently_sha_verified'
    if document.exists():
        inputs[str(document)] = sha256(document)
        if inputs[str(document)] != evidence['source']['sha256']:
            raise ValueError('Screenshot document changed after visual review.')
    elif allow_offline_transcription:
        document_status = 'previously_visually_reviewed_original_currently_unavailable'
    else:
        raise ValueError('Original Word unavailable; explicit offline-transcription option required.')
    job = next(row for row in evidence['jobs'] if row['id'] == '2026-05-27')
    hatch = evidence['hatch_observations'][job['hatch_observation']]
    events, parser = [], TimeLogParser()
    log_checksums = {row['file']: row['sha256'] for row in audit['timing_files']}
    for name in job['candidate_time_logs']:
        path = logs_root / name
        inputs[str(path)] = sha256(path)
        if inputs[str(path)] != log_checksums[name]:
            raise ValueError(f'Local log differs from previously CRC-checked archive member: {name}')
        events.extend(parser.parse(path, ParserContext()).events)
    measured = timing_components_ms(calibration_timing_payloads(events))
    if not measured:
        raise ValueError('No admitted measurements; no missing rows will be filled.')
    layers = np.asarray(sorted(measured), dtype=int)
    heights = origin + (layers - .5) * job['layer_thickness_mm']
    observed = np.asarray([measured[int(n)][0] / 1000 for n in layers])
    observed_pour = np.asarray([measured[int(n)][1] / 1000 for n in layers])
    hashes = {r['file']: r['sha256'] for r in audit['geometry'] if r['job_id'] == job['id']}
    # Mapping is explicit for these files, not inferred generically from s_ names.
    groups = {g['screenshot_group']: g for g in job['component_groups']
              if g['screenshot_group'] != 'reference'}
    mapping = {'corpus': {'body': groups['corpus']['part'],
        'support': 's_DKYuG.02.002.451311EMD Korpus - 33sht_16.stl',
        'extra': 's_s_DKYuG.02.002.451311EMD Korpus - 33sht_16_ex.stl'},
        'tail': {'body': groups['tail']['part'],
                 'support': 's_DKYuG.02.002.201000 Hvostovik v.01 - 15sht_7.stl'}}
    assignments = job['observed_laser_assignments']
    for key, group in groups.items():
        rows = [n for channel in assignments.values() for n in channel[key]]
        if len(rows) != group['copies'] or len(set(rows)) != len(rows):
            raise ValueError('Inconsistent screenshot quantities/laser assignments.')
    computed = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        tasks = {}
        for group, phases in mapping.items():
            for phase, filename in phases.items():
                path = Path(job['model_root']) / filename
                inputs[str(path)] = sha256(path)
                if inputs[str(path)] != hashes[filename]:
                    raise ValueError(f'Model changed after original audit: {filename}')
                future = (executor.submit(_body, path, heights, layers, hatch,
                    job['contours_enabled'], tabu) if phase == 'body'
                    else executor.submit(_tracks, path, heights))
                tasks[future] = (group, phase)
        for future in as_completed(tasks):
            key = tasks[future]
            computed[key] = future.result()
            progress(f'{key[0]}/{key[1]}: {len(layers)} heights, '
                     f'{computed[key]["elapsed_seconds"]:.1f} s')
    body_params = {'hatch_speed_mm_s': hatch['hatch_speed_mm_s'],
                   'jump_speed_mm_s': hatch['jump_speed_mm_s']}
    support_params = {'hatch_speed_mm_s': hatch['hatch_speed_mm_s'],
        'support_speed_mm_s': job['support']['speed_mm_s'],
        'jump_speed_mm_s': job['support']['jump_speed_mm_s']}
    body, support_mark, support_ordered, extra = {}, {}, {}, {}
    for group in mapping:
        body[group] = {scenario: _seconds(features, heights, job['layer_thickness_mm'], body_params)
            for scenario, features in computed[group, 'body']['features'].items()}
        tracks = computed[group, 'support']['features']
        support_ordered[group] = _seconds(tracks, heights, job['layer_thickness_mm'], support_params)
        marks = tracks.copy()
        marks[:, 2:4] = 0
        support_mark[group] = _seconds(marks, heights, job['layer_thickness_mm'], support_params)
        if (group, 'extra') in computed:
            extra[group] = _seconds(computed[group, 'extra']['features'], heights,
                job['layer_thickness_mm'], support_params)
        else:
            extra[group] = np.zeros(len(layers))
    def channel_sum(component):
        return {channel: sum(component[group] * len(rows[group]) for group in mapping)
                for channel, rows in assignments.items()}
    channels_body = {scenario: channel_sum({g: body[g][scenario] for g in mapping})
                     for scenario in next(iter(body.values()))}
    channels_mark, channels_ordered, channels_extra = (channel_sum(s) for s in
        (support_mark, support_ordered, extra))
    predictions = {}
    # Predeclared scenarios; no ranking, coefficient fit or automatic promotion.
    for scenario, channels in channels_body.items():
        predictions['body_only_' + scenario] = np.maximum.reduce(list(channels.values()))
    base = channels_body['variable_k_n_minus_1']
    for ordering, support in (('mark_only', channels_mark), ('entity_order_jumps', channels_ordered)):
        for activity, mask in (('every_layer', np.ones(len(layers))),
                               ('even_layers', layers % 2 == 0), ('odd_layers', layers % 2 == 1)):
            predictions[f'support_{ordering}_{activity}'] = np.maximum.reduce([
                base[channel] + support[channel] * mask for channel in base])
    summaries = {key: error_summary(value, observed) for key, value in predictions.items()}
    previous = component_comparison(audit['component_experiment'], events)
    config = {'job_id': job['id'], 'datum_z_mm_hypothesis': origin,
        'layer_thickness_mm': job['layer_thickness_mm'], 'tabu_angle_hypothesis': tabu,
        'body_recipe_hypothesis': body_params | {'hatch_distance_mm': hatch['hatch_distance_mm'],
            'base_angle_deg': hatch['hatch_angle_deg'], 'contours_enabled': job['contours_enabled']},
        'support_recipe_hypothesis': support_params, 'core_hatcher_offsets_mm': {
            'volume_offset': .08, 'spot_compensation': .06},
        'phase_file_mapping': mapping, 'laser_assignments': assignments,
        'support_activation': 'every/even/odd are hypotheses; Cones rule is NOT applied',
        'extra_stl': 'unclassified bridge candidate: separately measured; excluded from every main scenario',
        'extra_recipe_hypothesis': 'support speeds, section tracks, every layer; sensitivity only',
        'laser_schedule_hypothesis': 'max of summed assigned per-channel times; concurrent execution',
        'jump_delays_ms_hypothesis': 0.0, 'fitting_performed': False}
    rows = []
    for i, n in enumerate(layers):
        rows.append({'log_layer': int(n), 'height_mm_hypothesis': float(heights[i]),
            'observed_burn_seconds': float(observed[i]), 'observed_pour_seconds': float(observed_pour[i]),
            'components_per_copy': {g: {'body_seconds': {s: float(v[i]) for s, v in body[g].items()},
                'support_track_metrics': {**dict(zip(GEOMETRY_FEATURES,
                    computed[g, 'support']['features'][i].tolist(), strict=True)),
                    'n_tracks': computed[g, 'support']['track_counts'][i]},
                'support_mark_only_seconds': float(support_mark[g][i]),
                'support_entity_order_seconds': float(support_ordered[g][i]),
                'extra_proxy_seconds': float(extra[g][i])} for g in mapping},
            'channel_body_seconds': {c: float(v[i]) for c, v in base.items()},
            'channel_extra_proxy_seconds_if_enabled': {c: float(v[i]) for c, v in channels_extra.items()},
            'scenarios': {key: {'predicted_burn_seconds': float(value[i]),
                'residual_seconds': float(observed[i] - value[i])} for key, value in predictions.items()}})
    if any(sha256(Path(p)) != expected for p, expected in inputs.items()):
        raise ValueError('A source changed during calculation; discard this generation.')
    return {'schema_version': 1, 'scope': 'post_hoc_all_admitted_layer_phase_diagnostic_not_accuracy',
        'source': 'calculated', 'binary_execution_performed': False, 'production_estimator_changed': False,
        'document_verification_status': document_status, 'original_document_sha256': evidence['source']['sha256'],
        'input_sha256': inputs, 'config': config, 'measurement_summary': timing_summary(events),
        'previous_cohatch_diagnostic': previous, 'scenario_summaries': summaries, 'rows': rows,
        'component_elapsed_seconds': {f'{g}/{p}': v['elapsed_seconds'] for (g, p), v in computed.items()},
        'elapsed_seconds': perf_counter() - started, 'normal_whole_print_hours': None,
        'limitations': [
            'Screened measurements are partial components, not a complete normal whole-print target.',
            'Datum, slice index, same selected-model recipe for corpus, and two-channel concurrency are hypotheses.',
            'Recovered angle rule belongs to supplied 2.0.6.35; screenshot 2.0.6.033 transfer is unverified.',
            'Support STL entity order can contain large artificial jumps and is not a native path or upper/lower timing bound.',
            'Support mark-only excludes travel; hypothetical even/odd activation is not a verified SupportsStructure rule.',
            'Extra STL is not proven to be an active bridge with these track/speed settings; reference is excluded.',
            'Placement/rotations, entry/exit, inter-instance/phase jumps, skin/stripes, and hardware delays remain unknown.',
            'No model is fitted or promoted. Diagnostic sum agreement is not independent predictive accuracy.',
        ], 'provenance': build_provenance('rs320_separated_phase_probe', inputs=inputs, config=config,
            parser_versions={parser.name: parser.version},
            model_versions={'angle_rule_replay': RULE_VERSION, 'phase_probe': 'rs320-phase-probe-v1'},
            generated_by=f'local-offline-research@{socket.gethostname()}')}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    for name in ('evidence', 'audit', 'rules', 'binary', 'logs-root', 'output'):
        cli.add_argument('--' + name, type=Path, required=True)
    cli.add_argument('--build-origin-z-mm', type=float, required=True)
    cli.add_argument('--tabu-angle', type=int, required=True)
    cli.add_argument('--allow-offline-transcription', action='store_true')
    args = cli.parse_args()
    if args.output.exists():
        raise ValueError('Output exists; use a new report path, not overwrite prior evidence.')
    result = probe(evidence_path=args.evidence, audit_path=args.audit, rules_path=args.rules,
        binary_path=args.binary, logs_root=args.logs_root, origin=args.build_origin_z_mm,
        tabu=args.tabu_angle, allow_offline_transcription=args.allow_offline_transcription,
        progress=lambda message: print(message, file=sys.stderr, flush=True))
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({key: result[key] for key in ('measurement_summary', 'scenario_summaries',
        'component_elapsed_seconds', 'elapsed_seconds')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
