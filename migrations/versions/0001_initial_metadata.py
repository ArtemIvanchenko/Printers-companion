"""Initial schema — frozen snapshot of every table.

Revision ID: 0001_initial_metadata
Revises:
Create Date: 2026-04-27

This migration used to call ``Base.metadata.create_all()``. That made the schema
a fresh database receives depend on the *code version* rather than on the
migration chain: whatever the ORM models happened to declare got created, and
every later migration became a no-op guard because its columns already existed.
It also blinded the drift check in CI — on a fresh database the chain
reproduced the models by construction, so no diff could ever appear.

The DDL below is that same schema captured explicitly, so it is now a fixed
historical record. New columns and tables must arrive in their own migration.
Do not regenerate this file from the models.
"""

import sqlalchemy as sa
from alembic import op

revision = "0001_initial_metadata"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Pre-Alembic databases already have every table (they were created by an
    # earlier create_all at startup) — skip so we never issue DDL against
    # existing tables inside a PostgreSQL transaction.
    if sa.inspect(op.get_bind()).has_table("sessions"):
        return

    op.create_table('analysis_versions',
    sa.Column('analysis_version_id', sa.String(length=80), nullable=False),
    sa.Column('component', sa.String(length=120), nullable=False),
    sa.Column('version', sa.String(length=80), nullable=False),
    sa.Column('git_sha', sa.String(length=80), nullable=True),
    sa.Column('config_hash', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('analysis_version_id')
    )
    op.create_index(op.f('ix_analysis_versions_component'), 'analysis_versions', ['component'], unique=False)
    op.create_table('attachments',
    sa.Column('attachment_id', sa.String(length=80), nullable=False),
    sa.Column('owner_type', sa.String(length=80), nullable=False),
    sa.Column('owner_id', sa.String(length=80), nullable=False),
    sa.Column('file_type', sa.String(length=120), nullable=True),
    sa.Column('storage_uri', sa.String(length=700), nullable=False),
    sa.Column('uploaded_by', sa.String(length=120), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('hash', sa.String(length=128), nullable=True),
    sa.PrimaryKeyConstraint('attachment_id')
    )
    op.create_index(op.f('ix_attachments_owner_id'), 'attachments', ['owner_id'], unique=False)
    op.create_index(op.f('ix_attachments_owner_type'), 'attachments', ['owner_type'], unique=False)
    op.create_table('causal_links',
    sa.Column('causal_link_id', sa.String(length=80), nullable=False),
    sa.Column('source_id', sa.String(length=80), nullable=False),
    sa.Column('target_id', sa.String(length=80), nullable=False),
    sa.Column('relationship', sa.String(length=80), nullable=False),
    sa.Column('score', sa.Float(), nullable=False),
    sa.Column('evidence', sa.JSON(), nullable=False),
    sa.Column('data_quality', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('causal_link_id')
    )
    op.create_index(op.f('ix_causal_links_relationship'), 'causal_links', ['relationship'], unique=False)
    op.create_index(op.f('ix_causal_links_source_id'), 'causal_links', ['source_id'], unique=False)
    op.create_index(op.f('ix_causal_links_target_id'), 'causal_links', ['target_id'], unique=False)
    op.create_table('confirmed_knowledge',
    sa.Column('knowledge_id', sa.String(length=80), nullable=False),
    sa.Column('title', sa.String(length=240), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('scope', sa.JSON(), nullable=False),
    sa.Column('printer_profile', sa.String(length=120), nullable=True),
    sa.Column('applicable_materials', sa.JSON(), nullable=False),
    sa.Column('applicable_conditions', sa.JSON(), nullable=False),
    sa.Column('supporting_insights', sa.JSON(), nullable=False),
    sa.Column('confirmed_by', sa.String(length=120), nullable=False),
    sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('status', sa.String(length=80), nullable=False),
    sa.Column('rule_implications', sa.JSON(), nullable=False),
    sa.Column('report_implications', sa.JSON(), nullable=False),
    sa.Column('audit_trail', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('knowledge_id')
    )
    op.create_index(op.f('ix_confirmed_knowledge_printer_profile'), 'confirmed_knowledge', ['printer_profile'], unique=False)
    op.create_index(op.f('ix_confirmed_knowledge_status'), 'confirmed_knowledge', ['status'], unique=False)
    op.create_table('gas_cylinders',
    sa.Column('gas_cylinder_id', sa.String(length=160), nullable=False),
    sa.Column('gas_type', sa.String(length=80), nullable=False),
    sa.Column('installed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('removed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('initial_pressure', sa.Float(), nullable=True),
    sa.Column('pressure_unit', sa.String(length=40), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('gas_cylinder_id')
    )
    op.create_index(op.f('ix_gas_cylinders_gas_type'), 'gas_cylinders', ['gas_type'], unique=False)
    op.create_index(op.f('ix_gas_cylinders_installed_at'), 'gas_cylinders', ['installed_at'], unique=False)
    op.create_index(op.f('ix_gas_cylinders_removed_at'), 'gas_cylinders', ['removed_at'], unique=False)
    op.create_table('historical_analysis_verdicts',
    sa.Column('verdict_id', sa.String(length=80), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('analysis_window', sa.JSON(), nullable=False),
    sa.Column('max_iterations', sa.Integer(), nullable=False),
    sa.Column('completed_iterations', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=80), nullable=False),
    sa.Column('verdict', sa.String(length=80), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('new_insights', sa.JSON(), nullable=False),
    sa.Column('updated_insights', sa.JSON(), nullable=False),
    sa.Column('dismissed_candidates', sa.JSON(), nullable=False),
    sa.Column('counterexamples', sa.JSON(), nullable=False),
    sa.Column('missing_data', sa.JSON(), nullable=False),
    sa.Column('recommended_actions', sa.JSON(), nullable=False),
    sa.Column('affected_sessions', sa.JSON(), nullable=False),
    sa.Column('analysis_version', sa.String(length=80), nullable=False),
    sa.Column('evidence_links', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('verdict_id')
    )
    op.create_index(op.f('ix_historical_analysis_verdicts_status'), 'historical_analysis_verdicts', ['status'], unique=False)
    op.create_index(op.f('ix_historical_analysis_verdicts_verdict'), 'historical_analysis_verdicts', ['verdict'], unique=False)
    op.create_table('import_jobs',
    sa.Column('import_job_id', sa.String(length=80), nullable=False),
    sa.Column('source_path', sa.String(length=1000), nullable=False),
    sa.Column('source_name', sa.String(length=300), nullable=False),
    sa.Column('source_kind', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=80), nullable=False),
    sa.Column('detected_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('confirmation_deadline', sa.DateTime(timezone=True), nullable=True),
    sa.Column('confirmed_by', sa.String(length=120), nullable=True),
    sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('postponed_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ignored_by', sa.String(length=120), nullable=True),
    sa.Column('ignored_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_stability_check_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('file_snapshot', sa.JSON(), nullable=False),
    sa.Column('checksum_manifest', sa.JSON(), nullable=False),
    sa.Column('session_ids', sa.JSON(), nullable=False),
    sa.Column('report_ids', sa.JSON(), nullable=False),
    sa.Column('missing_context_questions', sa.JSON(), nullable=False),
    sa.Column('notification_log', sa.JSON(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('audit_trail', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('import_job_id')
    )
    op.create_index(op.f('ix_import_jobs_source_path'), 'import_jobs', ['source_path'], unique=False)
    op.create_index(op.f('ix_import_jobs_status'), 'import_jobs', ['status'], unique=False)
    op.create_index('ix_import_jobs_status_updated', 'import_jobs', ['status', 'updated_at'], unique=False)
    op.create_table('layer_ranges',
    sa.Column('layer_range_id', sa.String(length=80), nullable=False),
    sa.Column('start_layer', sa.Integer(), nullable=True),
    sa.Column('end_layer', sa.Integer(), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('layer_range_id')
    )
    op.create_table('machine_params',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('hatch_speed_mm_s', sa.Float(), nullable=True),
    sa.Column('contour_speed_mm_s', sa.Float(), nullable=True),
    sa.Column('hatch_distance_mm', sa.Float(), nullable=True),
    sa.Column('time_correction_factor', sa.Float(), nullable=True),
    sa.Column('correction_locked', sa.Boolean(), nullable=False),
    sa.Column('layer_thickness_mm', sa.Float(), nullable=True),
    sa.Column('laser_count', sa.Integer(), nullable=True),
    sa.Column('recoat_time_ms', sa.Float(), nullable=True),
    sa.Column('jump_speed_mm_s', sa.Float(), nullable=True),
    sa.Column('jump_delay_ms', sa.Float(), nullable=True),
    sa.Column('powder_cost_rub_per_kg', sa.Float(), nullable=True),
    sa.Column('gas_cost_rub_per_atm', sa.Float(), nullable=True),
    sa.Column('gas_atm_per_print', sa.Float(), nullable=True),
    sa.Column('filter_cost_rub', sa.Float(), nullable=True),
    sa.Column('filter_lifetime_hours', sa.Float(), nullable=True),
    sa.Column('platform_cost_rub', sa.Float(), nullable=True),
    sa.Column('material_densities', sa.JSON(), nullable=False),
    sa.Column('hatch_speeds_by_mat', sa.JSON(), nullable=False),
    sa.Column('time_correction_by_mat', sa.JSON(), nullable=False),
    sa.Column('build_area_cm2', sa.Float(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('machine_presets',
    sa.Column('preset_id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('name', sa.String(length=240), nullable=False),
    sa.Column('material', sa.String(length=120), nullable=False),
    sa.Column('layer_thickness_mm', sa.Float(), nullable=True),
    sa.Column('hatch_speed_mm_s', sa.Float(), nullable=True),
    sa.Column('contour_speed_mm_s', sa.Float(), nullable=True),
    sa.Column('hatch_distance_mm', sa.Float(), nullable=True),
    sa.Column('jump_speed_mm_s', sa.Float(), nullable=True),
    sa.Column('jump_delay_ms', sa.Float(), nullable=True),
    sa.Column('laser_power_w', sa.Float(), nullable=True),
    sa.Column('is_default', sa.Boolean(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('preset_id')
    )
    op.create_index(op.f('ix_machine_presets_material'), 'machine_presets', ['material'], unique=False)
    op.create_table('material_batches',
    sa.Column('material_batch_id', sa.String(length=80), nullable=False),
    sa.Column('material', sa.String(length=120), nullable=False),
    sa.Column('alloy', sa.String(length=120), nullable=True),
    sa.Column('batch_code', sa.String(length=160), nullable=False),
    sa.Column('supplier', sa.String(length=160), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('material_batch_id')
    )
    op.create_index(op.f('ix_material_batches_alloy'), 'material_batches', ['alloy'], unique=False)
    op.create_index(op.f('ix_material_batches_batch_code'), 'material_batches', ['batch_code'], unique=False)
    op.create_index(op.f('ix_material_batches_material'), 'material_batches', ['material'], unique=False)
    op.create_table('notification_outbox',
    sa.Column('notification_id', sa.String(length=80), nullable=False),
    sa.Column('channel', sa.String(length=80), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('buttons', sa.JSON(), nullable=False),
    sa.Column('metadata_json', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=80), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('notification_id')
    )
    op.create_index(op.f('ix_notification_outbox_channel'), 'notification_outbox', ['channel'], unique=False)
    op.create_index(op.f('ix_notification_outbox_status'), 'notification_outbox', ['status'], unique=False)
    op.create_table('printer_profiles',
    sa.Column('profile_id', sa.String(length=80), nullable=False),
    sa.Column('vendor', sa.String(length=120), nullable=False),
    sa.Column('model_family', sa.String(length=120), nullable=False),
    sa.Column('legacy_names', sa.JSON(), nullable=False),
    sa.Column('current_version', sa.String(length=80), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('profile_id')
    )
    op.create_table('tolerance_rules',
    sa.Column('rule_id', sa.String(length=80), nullable=False),
    sa.Column('feature_name', sa.String(length=80), nullable=False),
    sa.Column('min_value', sa.Float(), nullable=True),
    sa.Column('max_value', sa.Float(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('confirmed_by', sa.String(length=120), nullable=False),
    sa.Column('session_id_reference', sa.String(length=80), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('rule_id')
    )
    op.create_index(op.f('ix_tolerance_rules_feature_name'), 'tolerance_rules', ['feature_name'], unique=False)
    op.create_index(op.f('ix_tolerance_rules_session_id_reference'), 'tolerance_rules', ['session_id_reference'], unique=False)
    op.create_table('unknown_signal_reports',
    sa.Column('report_id', sa.String(length=80), nullable=False),
    sa.Column('field_name', sa.String(length=160), nullable=False),
    sa.Column('source_file_family', sa.String(length=80), nullable=False),
    sa.Column('occurrence_count', sa.Integer(), nullable=False),
    sa.Column('value_distribution', sa.JSON(), nullable=False),
    sa.Column('correlated_known_events', sa.JSON(), nullable=False),
    sa.Column('candidate_semantic_class', sa.String(length=120), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('examples', sa.JSON(), nullable=False),
    sa.Column('affected_sessions', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('report_id')
    )
    op.create_index(op.f('ix_unknown_signal_reports_field_name'), 'unknown_signal_reports', ['field_name'], unique=False)
    op.create_index(op.f('ix_unknown_signal_reports_source_file_family'), 'unknown_signal_reports', ['source_file_family'], unique=False)
    op.create_table('powder_usage_cycles',
    sa.Column('powder_cycle_id', sa.String(length=80), nullable=False),
    sa.Column('material_batch_id', sa.String(length=80), nullable=True),
    sa.Column('powder_batch', sa.String(length=160), nullable=True),
    sa.Column('reuse_count', sa.Integer(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('history', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['material_batch_id'], ['material_batches.material_batch_id'], ),
    sa.PrimaryKeyConstraint('powder_cycle_id')
    )
    op.create_index(op.f('ix_powder_usage_cycles_material_batch_id'), 'powder_usage_cycles', ['material_batch_id'], unique=False)
    op.create_index(op.f('ix_powder_usage_cycles_powder_batch'), 'powder_usage_cycles', ['powder_batch'], unique=False)
    op.create_table('printers',
    sa.Column('printer_id', sa.String(length=80), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('vendor', sa.String(length=120), nullable=False),
    sa.Column('model_family', sa.String(length=120), nullable=False),
    sa.Column('profile_id', sa.String(length=80), nullable=False),
    sa.Column('serial_number', sa.String(length=120), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['profile_id'], ['printer_profiles.profile_id'], ),
    sa.PrimaryKeyConstraint('printer_id')
    )
    op.create_index(op.f('ix_printers_profile_id'), 'printers', ['profile_id'], unique=False)
    op.create_table('profile_versions',
    sa.Column('version_id', sa.String(length=120), nullable=False),
    sa.Column('profile_id', sa.String(length=80), nullable=False),
    sa.Column('version', sa.String(length=80), nullable=False),
    sa.Column('mappings_hash', sa.String(length=128), nullable=True),
    sa.Column('rules_hash', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_by', sa.String(length=120), nullable=False),
    sa.ForeignKeyConstraint(['profile_id'], ['printer_profiles.profile_id'], ),
    sa.PrimaryKeyConstraint('version_id')
    )
    op.create_index(op.f('ix_profile_versions_profile_id'), 'profile_versions', ['profile_id'], unique=False)
    op.create_table('signal_dictionary_entries',
    sa.Column('signal_id', sa.String(length=120), nullable=False),
    sa.Column('profile_id', sa.String(length=80), nullable=False),
    sa.Column('raw_field_name', sa.String(length=160), nullable=False),
    sa.Column('canonical_name', sa.String(length=160), nullable=True),
    sa.Column('subsystem', sa.String(length=120), nullable=True),
    sa.Column('semantic_class', sa.String(length=120), nullable=True),
    sa.Column('unit', sa.String(length=80), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('active_status', sa.String(length=40), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('source', sa.String(length=120), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.String(length=80), nullable=False),
    sa.ForeignKeyConstraint(['profile_id'], ['printer_profiles.profile_id'], ),
    sa.PrimaryKeyConstraint('signal_id')
    )
    op.create_index(op.f('ix_signal_dictionary_entries_canonical_name'), 'signal_dictionary_entries', ['canonical_name'], unique=False)
    op.create_index(op.f('ix_signal_dictionary_entries_profile_id'), 'signal_dictionary_entries', ['profile_id'], unique=False)
    op.create_index(op.f('ix_signal_dictionary_entries_raw_field_name'), 'signal_dictionary_entries', ['raw_field_name'], unique=False)
    op.create_index(op.f('ix_signal_dictionary_entries_semantic_class'), 'signal_dictionary_entries', ['semantic_class'], unique=False)
    op.create_index(op.f('ix_signal_dictionary_entries_subsystem'), 'signal_dictionary_entries', ['subsystem'], unique=False)
    op.create_table('build_plates',
    sa.Column('plate_id', sa.String(length=80), nullable=False),
    sa.Column('printer_id', sa.String(length=80), nullable=True),
    sa.Column('identifier', sa.String(length=160), nullable=True),
    sa.Column('material', sa.String(length=120), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['printer_id'], ['printers.printer_id'], ),
    sa.PrimaryKeyConstraint('plate_id')
    )
    op.create_index(op.f('ix_build_plates_printer_id'), 'build_plates', ['printer_id'], unique=False)
    op.create_table('pattern_insights',
    sa.Column('insight_id', sa.String(length=80), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('analysis_window', sa.JSON(), nullable=False),
    sa.Column('printer_id', sa.String(length=80), nullable=True),
    sa.Column('scope_filters', sa.JSON(), nullable=False),
    sa.Column('insight_type', sa.String(length=120), nullable=False),
    sa.Column('title', sa.String(length=240), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('supporting_sessions', sa.JSON(), nullable=False),
    sa.Column('supporting_events', sa.JSON(), nullable=False),
    sa.Column('counterexamples', sa.JSON(), nullable=False),
    sa.Column('sample_size', sa.Integer(), nullable=False),
    sa.Column('effect_size', sa.Float(), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('causal_data_quality', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=80), nullable=False),
    sa.Column('generated_by', sa.String(length=120), nullable=False),
    sa.Column('analysis_version', sa.String(length=80), nullable=False),
    sa.Column('recommended_action', sa.Text(), nullable=True),
    sa.Column('audit_trail', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['printer_id'], ['printers.printer_id'], ),
    sa.PrimaryKeyConstraint('insight_id')
    )
    op.create_index(op.f('ix_pattern_insights_insight_type'), 'pattern_insights', ['insight_type'], unique=False)
    op.create_index(op.f('ix_pattern_insights_printer_id'), 'pattern_insights', ['printer_id'], unique=False)
    op.create_index(op.f('ix_pattern_insights_status'), 'pattern_insights', ['status'], unique=False)
    op.create_table('powder_preparation_events',
    sa.Column('prep_event_id', sa.String(length=80), nullable=False),
    sa.Column('powder_cycle_id', sa.String(length=80), nullable=True),
    sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.Column('event_type', sa.String(length=120), nullable=False),
    sa.Column('value', sa.String(length=240), nullable=True),
    sa.Column('unit', sa.String(length=80), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['powder_cycle_id'], ['powder_usage_cycles.powder_cycle_id'], ),
    sa.PrimaryKeyConstraint('prep_event_id')
    )
    op.create_index(op.f('ix_powder_preparation_events_event_type'), 'powder_preparation_events', ['event_type'], unique=False)
    op.create_index(op.f('ix_powder_preparation_events_powder_cycle_id'), 'powder_preparation_events', ['powder_cycle_id'], unique=False)
    op.create_index(op.f('ix_powder_preparation_events_timestamp'), 'powder_preparation_events', ['timestamp'], unique=False)
    op.create_table('sessions',
    sa.Column('session_id', sa.String(length=80), nullable=False),
    sa.Column('printer_id', sa.String(length=80), nullable=True),
    sa.Column('profile_id', sa.String(length=80), nullable=True),
    sa.Column('start_ts', sa.DateTime(timezone=True), nullable=True),
    sa.Column('end_ts', sa.DateTime(timezone=True), nullable=True),
    sa.Column('classification', sa.String(length=80), nullable=False),
    sa.Column('classification_confidence', sa.Float(), nullable=False),
    sa.Column('grouping_confidence', sa.Float(), nullable=False),
    sa.Column('status', sa.String(length=80), nullable=False),
    sa.Column('context', sa.JSON(), nullable=False),
    sa.Column('analysis_version', sa.String(length=80), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['printer_id'], ['printers.printer_id'], ),
    sa.ForeignKeyConstraint(['profile_id'], ['printer_profiles.profile_id'], ),
    sa.PrimaryKeyConstraint('session_id')
    )
    op.create_index(op.f('ix_sessions_end_ts'), 'sessions', ['end_ts'], unique=False)
    op.create_index(op.f('ix_sessions_printer_id'), 'sessions', ['printer_id'], unique=False)
    op.create_index(op.f('ix_sessions_profile_id'), 'sessions', ['profile_id'], unique=False)
    op.create_index(op.f('ix_sessions_start_ts'), 'sessions', ['start_ts'], unique=False)
    op.create_table('anomalies',
    sa.Column('anomaly_id', sa.String(length=80), nullable=False),
    sa.Column('session_id', sa.String(length=80), nullable=True),
    sa.Column('ts_start', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ts_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('layer_start', sa.Integer(), nullable=True),
    sa.Column('layer_end', sa.Integer(), nullable=True),
    sa.Column('anomaly_type', sa.String(length=160), nullable=False),
    sa.Column('severity', sa.String(length=80), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('evidence', sa.JSON(), nullable=False),
    sa.Column('features', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=80), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], ),
    sa.PrimaryKeyConstraint('anomaly_id')
    )
    op.create_index(op.f('ix_anomalies_anomaly_type'), 'anomalies', ['anomaly_type'], unique=False)
    op.create_index(op.f('ix_anomalies_session_id'), 'anomalies', ['session_id'], unique=False)
    op.create_index(op.f('ix_anomalies_ts_end'), 'anomalies', ['ts_end'], unique=False)
    op.create_index(op.f('ix_anomalies_ts_start'), 'anomalies', ['ts_start'], unique=False)
    op.create_table('build_jobs',
    sa.Column('build_id', sa.String(length=80), nullable=False),
    sa.Column('session_id', sa.String(length=80), nullable=False),
    sa.Column('job_name', sa.String(length=240), nullable=True),
    sa.Column('recipe', sa.String(length=240), nullable=True),
    sa.Column('layer_count', sa.Integer(), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], ),
    sa.PrimaryKeyConstraint('build_id')
    )
    op.create_index(op.f('ix_build_jobs_session_id'), 'build_jobs', ['session_id'], unique=False)
    op.create_table('hypotheses',
    sa.Column('hypothesis_id', sa.String(length=80), nullable=False),
    sa.Column('session_id', sa.String(length=80), nullable=True),
    sa.Column('title', sa.String(length=240), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('relationship', sa.String(length=80), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('uncertainty', sa.JSON(), nullable=False),
    sa.Column('supporting_evidence', sa.JSON(), nullable=False),
    sa.Column('contradictions', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], ),
    sa.PrimaryKeyConstraint('hypothesis_id')
    )
    op.create_index(op.f('ix_hypotheses_relationship'), 'hypotheses', ['relationship'], unique=False)
    op.create_index(op.f('ix_hypotheses_session_id'), 'hypotheses', ['session_id'], unique=False)
    op.create_table('layer_snapshots',
    sa.Column('layer_snapshot_id', sa.String(length=80), nullable=False),
    sa.Column('session_id', sa.String(length=80), nullable=False),
    sa.Column('layer', sa.Integer(), nullable=False),
    sa.Column('ts_start', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ts_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('features', sa.JSON(), nullable=False),
    sa.Column('context', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], ),
    sa.PrimaryKeyConstraint('layer_snapshot_id')
    )
    op.create_index(op.f('ix_layer_snapshots_layer'), 'layer_snapshots', ['layer'], unique=False)
    op.create_index(op.f('ix_layer_snapshots_session_id'), 'layer_snapshots', ['session_id'], unique=False)
    op.create_table('print_records',
    sa.Column('record_id', sa.String(length=80), nullable=False),
    sa.Column('name', sa.String(length=240), nullable=False),
    sa.Column('material', sa.String(length=120), nullable=False),
    sa.Column('session_id', sa.String(length=80), nullable=True),
    sa.Column('status', sa.String(length=40), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('printed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('powder_cost_rub_per_kg', sa.Float(), nullable=True),
    sa.Column('metadata_json', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], ),
    sa.PrimaryKeyConstraint('record_id')
    )
    op.create_index(op.f('ix_print_records_created_at'), 'print_records', ['created_at'], unique=False)
    op.create_index(op.f('ix_print_records_printed_at'), 'print_records', ['printed_at'], unique=False)
    op.create_index(op.f('ix_print_records_session_id'), 'print_records', ['session_id'], unique=False)
    op.create_table('reports',
    sa.Column('report_id', sa.String(length=80), nullable=False),
    sa.Column('session_id', sa.String(length=80), nullable=True),
    sa.Column('report_type', sa.String(length=80), nullable=False),
    sa.Column('storage_uri', sa.String(length=700), nullable=True),
    sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('generated_by', sa.String(length=120), nullable=False),
    sa.Column('version_metadata', sa.JSON(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], ),
    sa.PrimaryKeyConstraint('report_id')
    )
    op.create_index(op.f('ix_reports_report_type'), 'reports', ['report_type'], unique=False)
    op.create_index(op.f('ix_reports_session_id'), 'reports', ['session_id'], unique=False)
    op.create_table('segments',
    sa.Column('segment_id', sa.String(length=80), nullable=False),
    sa.Column('session_id', sa.String(length=80), nullable=False),
    sa.Column('phase', sa.String(length=120), nullable=False),
    sa.Column('ts_start', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ts_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('layer_start', sa.Integer(), nullable=True),
    sa.Column('layer_end', sa.Integer(), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('evidence', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], ),
    sa.PrimaryKeyConstraint('segment_id')
    )
    op.create_index(op.f('ix_segments_phase'), 'segments', ['phase'], unique=False)
    op.create_index(op.f('ix_segments_session_id'), 'segments', ['session_id'], unique=False)
    op.create_table('source_files',
    sa.Column('source_file_id', sa.String(length=80), nullable=False),
    sa.Column('session_id', sa.String(length=80), nullable=True),
    sa.Column('object_uri', sa.String(length=700), nullable=True),
    sa.Column('original_path', sa.String(length=1000), nullable=False),
    sa.Column('file_name', sa.String(length=300), nullable=False),
    sa.Column('checksum', sa.String(length=128), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('family', sa.String(length=80), nullable=False),
    sa.Column('role', sa.String(length=40), nullable=False),
    sa.Column('encoding', sa.String(length=80), nullable=True),
    sa.Column('data_quality_status', sa.String(length=80), nullable=False),
    sa.Column('first_ts', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_ts', sa.DateTime(timezone=True), nullable=True),
    sa.Column('parse_status', sa.String(length=80), nullable=False),
    sa.Column('metadata_json', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], ),
    sa.PrimaryKeyConstraint('source_file_id')
    )
    op.create_index('ix_source_files_checksum', 'source_files', ['checksum'], unique=False)
    op.create_index(op.f('ix_source_files_family'), 'source_files', ['family'], unique=False)
    op.create_index(op.f('ix_source_files_role'), 'source_files', ['role'], unique=False)
    op.create_index(op.f('ix_source_files_session_id'), 'source_files', ['session_id'], unique=False)
    op.create_table('canonical_events',
    sa.Column('event_id', sa.String(length=80), nullable=False),
    sa.Column('session_id', sa.String(length=80), nullable=True),
    sa.Column('ts', sa.DateTime(timezone=True), nullable=True),
    sa.Column('raw_timestamp', sa.String(length=160), nullable=True),
    sa.Column('ts_uncertainty', sa.Float(), nullable=False),
    sa.Column('layer', sa.Integer(), nullable=True),
    sa.Column('source_file_id', sa.String(length=80), nullable=True),
    sa.Column('source_line', sa.Integer(), nullable=True),
    sa.Column('source_offset', sa.Integer(), nullable=True),
    sa.Column('raw_excerpt', sa.Text(), nullable=True),
    sa.Column('subsystem', sa.String(length=120), nullable=True),
    sa.Column('phase', sa.String(length=120), nullable=True),
    sa.Column('event_type', sa.String(length=160), nullable=False),
    sa.Column('severity', sa.String(length=60), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('evidence_kind', sa.String(length=80), nullable=False),
    sa.Column('provenance', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], ),
    sa.ForeignKeyConstraint(['source_file_id'], ['source_files.source_file_id'], ),
    sa.PrimaryKeyConstraint('event_id')
    )
    op.create_index(op.f('ix_canonical_events_event_type'), 'canonical_events', ['event_type'], unique=False)
    op.create_index(op.f('ix_canonical_events_evidence_kind'), 'canonical_events', ['evidence_kind'], unique=False)
    op.create_index(op.f('ix_canonical_events_layer'), 'canonical_events', ['layer'], unique=False)
    op.create_index(op.f('ix_canonical_events_phase'), 'canonical_events', ['phase'], unique=False)
    op.create_index(op.f('ix_canonical_events_session_id'), 'canonical_events', ['session_id'], unique=False)
    op.create_index('ix_canonical_events_session_ts', 'canonical_events', ['session_id', 'ts'], unique=False)
    op.create_index(op.f('ix_canonical_events_source_file_id'), 'canonical_events', ['source_file_id'], unique=False)
    op.create_index(op.f('ix_canonical_events_subsystem'), 'canonical_events', ['subsystem'], unique=False)
    op.create_index(op.f('ix_canonical_events_ts'), 'canonical_events', ['ts'], unique=False)
    op.create_table('llm_runs',
    sa.Column('llm_run_id', sa.String(length=80), nullable=False),
    sa.Column('report_id', sa.String(length=80), nullable=True),
    sa.Column('provider', sa.String(length=80), nullable=False),
    sa.Column('model_name', sa.String(length=160), nullable=False),
    sa.Column('prompt_version', sa.String(length=80), nullable=False),
    sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.Column('token_estimates', sa.JSON(), nullable=False),
    sa.Column('success', sa.Boolean(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('request_metadata', sa.JSON(), nullable=False),
    sa.Column('response_metadata', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['report_id'], ['reports.report_id'], ),
    sa.PrimaryKeyConstraint('llm_run_id')
    )
    op.create_index(op.f('ix_llm_runs_provider'), 'llm_runs', ['provider'], unique=False)
    op.create_index(op.f('ix_llm_runs_report_id'), 'llm_runs', ['report_id'], unique=False)
    op.create_table('operator_events',
    sa.Column('event_id', sa.String(length=80), nullable=False),
    sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_by', sa.String(length=120), nullable=False),
    sa.Column('source_channel', sa.String(length=40), nullable=False),
    sa.Column('event_type', sa.String(length=120), nullable=False),
    sa.Column('printer_id', sa.String(length=80), nullable=True),
    sa.Column('session_id', sa.String(length=80), nullable=True),
    sa.Column('build_id', sa.String(length=80), nullable=True),
    sa.Column('layer', sa.Integer(), nullable=True),
    sa.Column('material', sa.String(length=120), nullable=True),
    sa.Column('powder_batch', sa.String(length=160), nullable=True),
    sa.Column('gas_type', sa.String(length=80), nullable=True),
    sa.Column('gas_cylinder_id', sa.String(length=160), nullable=True),
    sa.Column('component', sa.String(length=160), nullable=True),
    sa.Column('action', sa.String(length=160), nullable=True),
    sa.Column('value', sa.String(length=240), nullable=True),
    sa.Column('unit', sa.String(length=80), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('attachments', sa.JSON(), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('verification_status', sa.String(length=80), nullable=False),
    sa.Column('linked_machine_events', sa.JSON(), nullable=False),
    sa.Column('audit_trail', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['build_id'], ['build_jobs.build_id'], ),
    sa.ForeignKeyConstraint(['printer_id'], ['printers.printer_id'], ),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], ),
    sa.PrimaryKeyConstraint('event_id')
    )
    op.create_index(op.f('ix_operator_events_build_id'), 'operator_events', ['build_id'], unique=False)
    op.create_index(op.f('ix_operator_events_event_type'), 'operator_events', ['event_type'], unique=False)
    op.create_index(op.f('ix_operator_events_printer_id'), 'operator_events', ['printer_id'], unique=False)
    op.create_index(op.f('ix_operator_events_session_id'), 'operator_events', ['session_id'], unique=False)
    op.create_index(op.f('ix_operator_events_source_channel'), 'operator_events', ['source_channel'], unique=False)
    op.create_index(op.f('ix_operator_events_timestamp'), 'operator_events', ['timestamp'], unique=False)
    op.create_index(op.f('ix_operator_events_verification_status'), 'operator_events', ['verification_status'], unique=False)
    op.create_table('parse_diagnostics',
    sa.Column('diagnostic_id', sa.String(length=80), nullable=False),
    sa.Column('source_file_id', sa.String(length=80), nullable=True),
    sa.Column('session_id', sa.String(length=80), nullable=True),
    sa.Column('parser_name', sa.String(length=160), nullable=False),
    sa.Column('parser_version', sa.String(length=80), nullable=False),
    sa.Column('severity', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=120), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('source_line', sa.Integer(), nullable=True),
    sa.Column('source_offset', sa.Integer(), nullable=True),
    sa.Column('context', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], ),
    sa.ForeignKeyConstraint(['source_file_id'], ['source_files.source_file_id'], ),
    sa.PrimaryKeyConstraint('diagnostic_id')
    )
    op.create_index(op.f('ix_parse_diagnostics_session_id'), 'parse_diagnostics', ['session_id'], unique=False)
    op.create_index(op.f('ix_parse_diagnostics_source_file_id'), 'parse_diagnostics', ['source_file_id'], unique=False)
    op.create_table('parts',
    sa.Column('part_id', sa.String(length=80), nullable=False),
    sa.Column('build_id', sa.String(length=80), nullable=True),
    sa.Column('name', sa.String(length=240), nullable=True),
    sa.Column('geometry_ref', sa.String(length=500), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['build_id'], ['build_jobs.build_id'], ),
    sa.PrimaryKeyConstraint('part_id')
    )
    op.create_index(op.f('ix_parts_build_id'), 'parts', ['build_id'], unique=False)
    op.create_table('print_record_files',
    sa.Column('file_id', sa.String(length=80), nullable=False),
    sa.Column('record_id', sa.String(length=80), nullable=False),
    sa.Column('object_uri', sa.String(length=700), nullable=False),
    sa.Column('file_name', sa.String(length=300), nullable=False),
    sa.Column('file_type', sa.String(length=40), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('checksum', sa.String(length=128), nullable=False),
    sa.Column('uploaded_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['record_id'], ['print_records.record_id'], ),
    sa.PrimaryKeyConstraint('file_id')
    )
    op.create_index(op.f('ix_print_record_files_checksum'), 'print_record_files', ['checksum'], unique=False)
    op.create_index(op.f('ix_print_record_files_file_type'), 'print_record_files', ['file_type'], unique=False)
    op.create_index(op.f('ix_print_record_files_record_id'), 'print_record_files', ['record_id'], unique=False)
    op.create_table('state_transitions',
    sa.Column('transition_id', sa.String(length=80), nullable=False),
    sa.Column('session_id', sa.String(length=80), nullable=True),
    sa.Column('ts_start', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ts_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duration_sec', sa.Float(), nullable=True),
    sa.Column('changed_columns', sa.JSON(), nullable=False),
    sa.Column('previous_state', sa.JSON(), nullable=False),
    sa.Column('new_state', sa.JSON(), nullable=False),
    sa.Column('subsystem', sa.String(length=120), nullable=True),
    sa.Column('source_file_id', sa.String(length=80), nullable=True),
    sa.Column('source_offset_start', sa.Integer(), nullable=True),
    sa.Column('source_offset_end', sa.Integer(), nullable=True),
    sa.Column('raw_excerpt_sample', sa.Text(), nullable=True),
    sa.Column('parser_version', sa.String(length=80), nullable=False),
    sa.Column('profile_version', sa.String(length=80), nullable=True),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], ),
    sa.ForeignKeyConstraint(['source_file_id'], ['source_files.source_file_id'], ),
    sa.PrimaryKeyConstraint('transition_id')
    )
    op.create_index(op.f('ix_state_transitions_session_id'), 'state_transitions', ['session_id'], unique=False)
    op.create_index('ix_state_transitions_session_ts', 'state_transitions', ['session_id', 'ts_start'], unique=False)
    op.create_index(op.f('ix_state_transitions_source_file_id'), 'state_transitions', ['source_file_id'], unique=False)
    op.create_index(op.f('ix_state_transitions_subsystem'), 'state_transitions', ['subsystem'], unique=False)
    op.create_index(op.f('ix_state_transitions_ts_end'), 'state_transitions', ['ts_end'], unique=False)
    op.create_index(op.f('ix_state_transitions_ts_start'), 'state_transitions', ['ts_start'], unique=False)
    op.create_table('component_state_timeline',
    sa.Column('state_id', sa.String(length=80), nullable=False),
    sa.Column('printer_id', sa.String(length=80), nullable=True),
    sa.Column('component', sa.String(length=160), nullable=False),
    sa.Column('state', sa.String(length=160), nullable=False),
    sa.Column('valid_from', sa.DateTime(timezone=True), nullable=False),
    sa.Column('valid_to', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source_event_id', sa.String(length=80), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['printer_id'], ['printers.printer_id'], ),
    sa.ForeignKeyConstraint(['source_event_id'], ['operator_events.event_id'], ),
    sa.PrimaryKeyConstraint('state_id')
    )
    op.create_index(op.f('ix_component_state_timeline_component'), 'component_state_timeline', ['component'], unique=False)
    op.create_index(op.f('ix_component_state_timeline_printer_id'), 'component_state_timeline', ['printer_id'], unique=False)
    op.create_index(op.f('ix_component_state_timeline_state'), 'component_state_timeline', ['state'], unique=False)
    op.create_index(op.f('ix_component_state_timeline_valid_from'), 'component_state_timeline', ['valid_from'], unique=False)
    op.create_index(op.f('ix_component_state_timeline_valid_to'), 'component_state_timeline', ['valid_to'], unique=False)
    op.create_table('maintenance_records',
    sa.Column('maintenance_id', sa.String(length=80), nullable=False),
    sa.Column('printer_id', sa.String(length=80), nullable=True),
    sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.Column('component', sa.String(length=160), nullable=False),
    sa.Column('action', sa.String(length=160), nullable=False),
    sa.Column('source_event_id', sa.String(length=80), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['printer_id'], ['printers.printer_id'], ),
    sa.ForeignKeyConstraint(['source_event_id'], ['operator_events.event_id'], ),
    sa.PrimaryKeyConstraint('maintenance_id')
    )
    op.create_index(op.f('ix_maintenance_records_action'), 'maintenance_records', ['action'], unique=False)
    op.create_index(op.f('ix_maintenance_records_component'), 'maintenance_records', ['component'], unique=False)
    op.create_index(op.f('ix_maintenance_records_printer_id'), 'maintenance_records', ['printer_id'], unique=False)
    op.create_index(op.f('ix_maintenance_records_timestamp'), 'maintenance_records', ['timestamp'], unique=False)
    op.create_table('operator_event_audit_records',
    sa.Column('audit_id', sa.String(length=80), nullable=False),
    sa.Column('event_id', sa.String(length=80), nullable=False),
    sa.Column('action', sa.String(length=120), nullable=False),
    sa.Column('actor', sa.String(length=120), nullable=False),
    sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.Column('before', sa.JSON(), nullable=True),
    sa.Column('after', sa.JSON(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['event_id'], ['operator_events.event_id'], ),
    sa.PrimaryKeyConstraint('audit_id')
    )
    op.create_index(op.f('ix_operator_event_audit_records_event_id'), 'operator_event_audit_records', ['event_id'], unique=False)
    op.create_table('operator_journal_entries',
    sa.Column('journal_entry_id', sa.String(length=80), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('source_channel', sa.String(length=40), nullable=False),
    sa.Column('created_by', sa.String(length=120), nullable=False),
    sa.Column('printer_id', sa.String(length=80), nullable=True),
    sa.Column('session_id', sa.String(length=80), nullable=True),
    sa.Column('project_id', sa.String(length=160), nullable=True),
    sa.Column('platform_id', sa.String(length=160), nullable=True),
    sa.Column('duplication_group_id', sa.String(length=120), nullable=True),
    sa.Column('entry_kind', sa.String(length=80), nullable=False),
    sa.Column('raw_text', sa.Text(), nullable=True),
    sa.Column('normalized_text', sa.Text(), nullable=True),
    sa.Column('voice_attachment', sa.JSON(), nullable=True),
    sa.Column('transcription', sa.JSON(), nullable=False),
    sa.Column('operator_event_id', sa.String(length=80), nullable=True),
    sa.Column('status', sa.String(length=80), nullable=False),
    sa.Column('duplicate_targets', sa.JSON(), nullable=False),
    sa.Column('export_payload', sa.JSON(), nullable=False),
    sa.Column('audit_trail', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['operator_event_id'], ['operator_events.event_id'], ),
    sa.ForeignKeyConstraint(['printer_id'], ['printers.printer_id'], ),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], ),
    sa.PrimaryKeyConstraint('journal_entry_id')
    )
    op.create_index('ix_operator_journal_created_project', 'operator_journal_entries', ['created_at', 'project_id'], unique=False)
    op.create_index(op.f('ix_operator_journal_entries_created_at'), 'operator_journal_entries', ['created_at'], unique=False)
    op.create_index(op.f('ix_operator_journal_entries_created_by'), 'operator_journal_entries', ['created_by'], unique=False)
    op.create_index(op.f('ix_operator_journal_entries_duplication_group_id'), 'operator_journal_entries', ['duplication_group_id'], unique=False)
    op.create_index(op.f('ix_operator_journal_entries_entry_kind'), 'operator_journal_entries', ['entry_kind'], unique=False)
    op.create_index(op.f('ix_operator_journal_entries_operator_event_id'), 'operator_journal_entries', ['operator_event_id'], unique=False)
    op.create_index(op.f('ix_operator_journal_entries_platform_id'), 'operator_journal_entries', ['platform_id'], unique=False)
    op.create_index(op.f('ix_operator_journal_entries_printer_id'), 'operator_journal_entries', ['printer_id'], unique=False)
    op.create_index(op.f('ix_operator_journal_entries_project_id'), 'operator_journal_entries', ['project_id'], unique=False)
    op.create_index(op.f('ix_operator_journal_entries_session_id'), 'operator_journal_entries', ['session_id'], unique=False)
    op.create_index(op.f('ix_operator_journal_entries_source_channel'), 'operator_journal_entries', ['source_channel'], unique=False)
    op.create_index(op.f('ix_operator_journal_entries_status'), 'operator_journal_entries', ['status'], unique=False)
    op.create_table('part_placements',
    sa.Column('placement_id', sa.String(length=80), nullable=False),
    sa.Column('part_id', sa.String(length=80), nullable=False),
    sa.Column('plate_id', sa.String(length=80), nullable=True),
    sa.Column('x', sa.Float(), nullable=True),
    sa.Column('y', sa.Float(), nullable=True),
    sa.Column('rotation_deg', sa.Float(), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['part_id'], ['parts.part_id'], ),
    sa.ForeignKeyConstraint(['plate_id'], ['build_plates.plate_id'], ),
    sa.PrimaryKeyConstraint('placement_id')
    )
    op.create_index(op.f('ix_part_placements_part_id'), 'part_placements', ['part_id'], unique=False)
    op.create_index(op.f('ix_part_placements_plate_id'), 'part_placements', ['plate_id'], unique=False)
    op.create_table('production_context_snapshots',
    sa.Column('snapshot_id', sa.String(length=80), nullable=False),
    sa.Column('printer_id', sa.String(length=80), nullable=True),
    sa.Column('session_id', sa.String(length=80), nullable=True),
    sa.Column('valid_from', sa.DateTime(timezone=True), nullable=False),
    sa.Column('valid_to', sa.DateTime(timezone=True), nullable=True),
    sa.Column('context', sa.JSON(), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('source_event_id', sa.String(length=80), nullable=True),
    sa.Column('conflict_flags', sa.JSON(), nullable=False),
    sa.Column('audit_trail', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['printer_id'], ['printers.printer_id'], ),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], ),
    sa.ForeignKeyConstraint(['source_event_id'], ['operator_events.event_id'], ),
    sa.PrimaryKeyConstraint('snapshot_id')
    )
    op.create_index(op.f('ix_production_context_snapshots_printer_id'), 'production_context_snapshots', ['printer_id'], unique=False)
    op.create_index(op.f('ix_production_context_snapshots_session_id'), 'production_context_snapshots', ['session_id'], unique=False)
    op.create_index(op.f('ix_production_context_snapshots_valid_from'), 'production_context_snapshots', ['valid_from'], unique=False)
    op.create_index(op.f('ix_production_context_snapshots_valid_to'), 'production_context_snapshots', ['valid_to'], unique=False)
    op.create_table('quality_outcomes',
    sa.Column('outcome_id', sa.String(length=80), nullable=False),
    sa.Column('session_id', sa.String(length=80), nullable=True),
    sa.Column('build_id', sa.String(length=80), nullable=True),
    sa.Column('part_id', sa.String(length=80), nullable=True),
    sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.Column('inspection_type', sa.String(length=80), nullable=False),
    sa.Column('result', sa.String(length=80), nullable=False),
    sa.Column('defect_type', sa.String(length=120), nullable=True),
    sa.Column('defect_location', sa.String(length=240), nullable=True),
    sa.Column('layer_range', sa.JSON(), nullable=True),
    sa.Column('severity', sa.String(length=80), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('attachments', sa.JSON(), nullable=False),
    sa.Column('created_by', sa.String(length=120), nullable=False),
    sa.Column('evidence_links', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['build_id'], ['build_jobs.build_id'], ),
    sa.ForeignKeyConstraint(['part_id'], ['parts.part_id'], ),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], ),
    sa.PrimaryKeyConstraint('outcome_id')
    )
    op.create_index(op.f('ix_quality_outcomes_build_id'), 'quality_outcomes', ['build_id'], unique=False)
    op.create_index(op.f('ix_quality_outcomes_defect_type'), 'quality_outcomes', ['defect_type'], unique=False)
    op.create_index(op.f('ix_quality_outcomes_inspection_type'), 'quality_outcomes', ['inspection_type'], unique=False)
    op.create_index(op.f('ix_quality_outcomes_part_id'), 'quality_outcomes', ['part_id'], unique=False)
    op.create_index(op.f('ix_quality_outcomes_result'), 'quality_outcomes', ['result'], unique=False)
    op.create_index(op.f('ix_quality_outcomes_session_id'), 'quality_outcomes', ['session_id'], unique=False)
    op.create_index(op.f('ix_quality_outcomes_timestamp'), 'quality_outcomes', ['timestamp'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_quality_outcomes_timestamp'), table_name='quality_outcomes')
    op.drop_index(op.f('ix_quality_outcomes_session_id'), table_name='quality_outcomes')
    op.drop_index(op.f('ix_quality_outcomes_result'), table_name='quality_outcomes')
    op.drop_index(op.f('ix_quality_outcomes_part_id'), table_name='quality_outcomes')
    op.drop_index(op.f('ix_quality_outcomes_inspection_type'), table_name='quality_outcomes')
    op.drop_index(op.f('ix_quality_outcomes_defect_type'), table_name='quality_outcomes')
    op.drop_index(op.f('ix_quality_outcomes_build_id'), table_name='quality_outcomes')
    op.drop_table('quality_outcomes')
    op.drop_index(op.f('ix_production_context_snapshots_valid_to'), table_name='production_context_snapshots')
    op.drop_index(op.f('ix_production_context_snapshots_valid_from'), table_name='production_context_snapshots')
    op.drop_index(op.f('ix_production_context_snapshots_session_id'), table_name='production_context_snapshots')
    op.drop_index(op.f('ix_production_context_snapshots_printer_id'), table_name='production_context_snapshots')
    op.drop_table('production_context_snapshots')
    op.drop_index(op.f('ix_part_placements_plate_id'), table_name='part_placements')
    op.drop_index(op.f('ix_part_placements_part_id'), table_name='part_placements')
    op.drop_table('part_placements')
    op.drop_index(op.f('ix_operator_journal_entries_status'), table_name='operator_journal_entries')
    op.drop_index(op.f('ix_operator_journal_entries_source_channel'), table_name='operator_journal_entries')
    op.drop_index(op.f('ix_operator_journal_entries_session_id'), table_name='operator_journal_entries')
    op.drop_index(op.f('ix_operator_journal_entries_project_id'), table_name='operator_journal_entries')
    op.drop_index(op.f('ix_operator_journal_entries_printer_id'), table_name='operator_journal_entries')
    op.drop_index(op.f('ix_operator_journal_entries_platform_id'), table_name='operator_journal_entries')
    op.drop_index(op.f('ix_operator_journal_entries_operator_event_id'), table_name='operator_journal_entries')
    op.drop_index(op.f('ix_operator_journal_entries_entry_kind'), table_name='operator_journal_entries')
    op.drop_index(op.f('ix_operator_journal_entries_duplication_group_id'), table_name='operator_journal_entries')
    op.drop_index(op.f('ix_operator_journal_entries_created_by'), table_name='operator_journal_entries')
    op.drop_index(op.f('ix_operator_journal_entries_created_at'), table_name='operator_journal_entries')
    op.drop_index('ix_operator_journal_created_project', table_name='operator_journal_entries')
    op.drop_table('operator_journal_entries')
    op.drop_index(op.f('ix_operator_event_audit_records_event_id'), table_name='operator_event_audit_records')
    op.drop_table('operator_event_audit_records')
    op.drop_index(op.f('ix_maintenance_records_timestamp'), table_name='maintenance_records')
    op.drop_index(op.f('ix_maintenance_records_printer_id'), table_name='maintenance_records')
    op.drop_index(op.f('ix_maintenance_records_component'), table_name='maintenance_records')
    op.drop_index(op.f('ix_maintenance_records_action'), table_name='maintenance_records')
    op.drop_table('maintenance_records')
    op.drop_index(op.f('ix_component_state_timeline_valid_to'), table_name='component_state_timeline')
    op.drop_index(op.f('ix_component_state_timeline_valid_from'), table_name='component_state_timeline')
    op.drop_index(op.f('ix_component_state_timeline_state'), table_name='component_state_timeline')
    op.drop_index(op.f('ix_component_state_timeline_printer_id'), table_name='component_state_timeline')
    op.drop_index(op.f('ix_component_state_timeline_component'), table_name='component_state_timeline')
    op.drop_table('component_state_timeline')
    op.drop_index(op.f('ix_state_transitions_ts_start'), table_name='state_transitions')
    op.drop_index(op.f('ix_state_transitions_ts_end'), table_name='state_transitions')
    op.drop_index(op.f('ix_state_transitions_subsystem'), table_name='state_transitions')
    op.drop_index(op.f('ix_state_transitions_source_file_id'), table_name='state_transitions')
    op.drop_index('ix_state_transitions_session_ts', table_name='state_transitions')
    op.drop_index(op.f('ix_state_transitions_session_id'), table_name='state_transitions')
    op.drop_table('state_transitions')
    op.drop_index(op.f('ix_print_record_files_record_id'), table_name='print_record_files')
    op.drop_index(op.f('ix_print_record_files_file_type'), table_name='print_record_files')
    op.drop_index(op.f('ix_print_record_files_checksum'), table_name='print_record_files')
    op.drop_table('print_record_files')
    op.drop_index(op.f('ix_parts_build_id'), table_name='parts')
    op.drop_table('parts')
    op.drop_index(op.f('ix_parse_diagnostics_source_file_id'), table_name='parse_diagnostics')
    op.drop_index(op.f('ix_parse_diagnostics_session_id'), table_name='parse_diagnostics')
    op.drop_table('parse_diagnostics')
    op.drop_index(op.f('ix_operator_events_verification_status'), table_name='operator_events')
    op.drop_index(op.f('ix_operator_events_timestamp'), table_name='operator_events')
    op.drop_index(op.f('ix_operator_events_source_channel'), table_name='operator_events')
    op.drop_index(op.f('ix_operator_events_session_id'), table_name='operator_events')
    op.drop_index(op.f('ix_operator_events_printer_id'), table_name='operator_events')
    op.drop_index(op.f('ix_operator_events_event_type'), table_name='operator_events')
    op.drop_index(op.f('ix_operator_events_build_id'), table_name='operator_events')
    op.drop_table('operator_events')
    op.drop_index(op.f('ix_llm_runs_report_id'), table_name='llm_runs')
    op.drop_index(op.f('ix_llm_runs_provider'), table_name='llm_runs')
    op.drop_table('llm_runs')
    op.drop_index(op.f('ix_canonical_events_ts'), table_name='canonical_events')
    op.drop_index(op.f('ix_canonical_events_subsystem'), table_name='canonical_events')
    op.drop_index(op.f('ix_canonical_events_source_file_id'), table_name='canonical_events')
    op.drop_index('ix_canonical_events_session_ts', table_name='canonical_events')
    op.drop_index(op.f('ix_canonical_events_session_id'), table_name='canonical_events')
    op.drop_index(op.f('ix_canonical_events_phase'), table_name='canonical_events')
    op.drop_index(op.f('ix_canonical_events_layer'), table_name='canonical_events')
    op.drop_index(op.f('ix_canonical_events_evidence_kind'), table_name='canonical_events')
    op.drop_index(op.f('ix_canonical_events_event_type'), table_name='canonical_events')
    op.drop_table('canonical_events')
    op.drop_index(op.f('ix_source_files_session_id'), table_name='source_files')
    op.drop_index(op.f('ix_source_files_role'), table_name='source_files')
    op.drop_index(op.f('ix_source_files_family'), table_name='source_files')
    op.drop_index('ix_source_files_checksum', table_name='source_files')
    op.drop_table('source_files')
    op.drop_index(op.f('ix_segments_session_id'), table_name='segments')
    op.drop_index(op.f('ix_segments_phase'), table_name='segments')
    op.drop_table('segments')
    op.drop_index(op.f('ix_reports_session_id'), table_name='reports')
    op.drop_index(op.f('ix_reports_report_type'), table_name='reports')
    op.drop_table('reports')
    op.drop_index(op.f('ix_print_records_session_id'), table_name='print_records')
    op.drop_index(op.f('ix_print_records_printed_at'), table_name='print_records')
    op.drop_index(op.f('ix_print_records_created_at'), table_name='print_records')
    op.drop_table('print_records')
    op.drop_index(op.f('ix_layer_snapshots_session_id'), table_name='layer_snapshots')
    op.drop_index(op.f('ix_layer_snapshots_layer'), table_name='layer_snapshots')
    op.drop_table('layer_snapshots')
    op.drop_index(op.f('ix_hypotheses_session_id'), table_name='hypotheses')
    op.drop_index(op.f('ix_hypotheses_relationship'), table_name='hypotheses')
    op.drop_table('hypotheses')
    op.drop_index(op.f('ix_build_jobs_session_id'), table_name='build_jobs')
    op.drop_table('build_jobs')
    op.drop_index(op.f('ix_anomalies_ts_start'), table_name='anomalies')
    op.drop_index(op.f('ix_anomalies_ts_end'), table_name='anomalies')
    op.drop_index(op.f('ix_anomalies_session_id'), table_name='anomalies')
    op.drop_index(op.f('ix_anomalies_anomaly_type'), table_name='anomalies')
    op.drop_table('anomalies')
    op.drop_index(op.f('ix_sessions_start_ts'), table_name='sessions')
    op.drop_index(op.f('ix_sessions_profile_id'), table_name='sessions')
    op.drop_index(op.f('ix_sessions_printer_id'), table_name='sessions')
    op.drop_index(op.f('ix_sessions_end_ts'), table_name='sessions')
    op.drop_table('sessions')
    op.drop_index(op.f('ix_powder_preparation_events_timestamp'), table_name='powder_preparation_events')
    op.drop_index(op.f('ix_powder_preparation_events_powder_cycle_id'), table_name='powder_preparation_events')
    op.drop_index(op.f('ix_powder_preparation_events_event_type'), table_name='powder_preparation_events')
    op.drop_table('powder_preparation_events')
    op.drop_index(op.f('ix_pattern_insights_status'), table_name='pattern_insights')
    op.drop_index(op.f('ix_pattern_insights_printer_id'), table_name='pattern_insights')
    op.drop_index(op.f('ix_pattern_insights_insight_type'), table_name='pattern_insights')
    op.drop_table('pattern_insights')
    op.drop_index(op.f('ix_build_plates_printer_id'), table_name='build_plates')
    op.drop_table('build_plates')
    op.drop_index(op.f('ix_signal_dictionary_entries_subsystem'), table_name='signal_dictionary_entries')
    op.drop_index(op.f('ix_signal_dictionary_entries_semantic_class'), table_name='signal_dictionary_entries')
    op.drop_index(op.f('ix_signal_dictionary_entries_raw_field_name'), table_name='signal_dictionary_entries')
    op.drop_index(op.f('ix_signal_dictionary_entries_profile_id'), table_name='signal_dictionary_entries')
    op.drop_index(op.f('ix_signal_dictionary_entries_canonical_name'), table_name='signal_dictionary_entries')
    op.drop_table('signal_dictionary_entries')
    op.drop_index(op.f('ix_profile_versions_profile_id'), table_name='profile_versions')
    op.drop_table('profile_versions')
    op.drop_index(op.f('ix_printers_profile_id'), table_name='printers')
    op.drop_table('printers')
    op.drop_index(op.f('ix_powder_usage_cycles_powder_batch'), table_name='powder_usage_cycles')
    op.drop_index(op.f('ix_powder_usage_cycles_material_batch_id'), table_name='powder_usage_cycles')
    op.drop_table('powder_usage_cycles')
    op.drop_index(op.f('ix_unknown_signal_reports_source_file_family'), table_name='unknown_signal_reports')
    op.drop_index(op.f('ix_unknown_signal_reports_field_name'), table_name='unknown_signal_reports')
    op.drop_table('unknown_signal_reports')
    op.drop_index(op.f('ix_tolerance_rules_session_id_reference'), table_name='tolerance_rules')
    op.drop_index(op.f('ix_tolerance_rules_feature_name'), table_name='tolerance_rules')
    op.drop_table('tolerance_rules')
    op.drop_table('printer_profiles')
    op.drop_index(op.f('ix_notification_outbox_status'), table_name='notification_outbox')
    op.drop_index(op.f('ix_notification_outbox_channel'), table_name='notification_outbox')
    op.drop_table('notification_outbox')
    op.drop_index(op.f('ix_material_batches_material'), table_name='material_batches')
    op.drop_index(op.f('ix_material_batches_batch_code'), table_name='material_batches')
    op.drop_index(op.f('ix_material_batches_alloy'), table_name='material_batches')
    op.drop_table('material_batches')
    op.drop_index(op.f('ix_machine_presets_material'), table_name='machine_presets')
    op.drop_table('machine_presets')
    op.drop_table('machine_params')
    op.drop_table('layer_ranges')
    op.drop_index('ix_import_jobs_status_updated', table_name='import_jobs')
    op.drop_index(op.f('ix_import_jobs_status'), table_name='import_jobs')
    op.drop_index(op.f('ix_import_jobs_source_path'), table_name='import_jobs')
    op.drop_table('import_jobs')
    op.drop_index(op.f('ix_historical_analysis_verdicts_verdict'), table_name='historical_analysis_verdicts')
    op.drop_index(op.f('ix_historical_analysis_verdicts_status'), table_name='historical_analysis_verdicts')
    op.drop_table('historical_analysis_verdicts')
    op.drop_index(op.f('ix_gas_cylinders_removed_at'), table_name='gas_cylinders')
    op.drop_index(op.f('ix_gas_cylinders_installed_at'), table_name='gas_cylinders')
    op.drop_index(op.f('ix_gas_cylinders_gas_type'), table_name='gas_cylinders')
    op.drop_table('gas_cylinders')
    op.drop_index(op.f('ix_confirmed_knowledge_status'), table_name='confirmed_knowledge')
    op.drop_index(op.f('ix_confirmed_knowledge_printer_profile'), table_name='confirmed_knowledge')
    op.drop_table('confirmed_knowledge')
    op.drop_index(op.f('ix_causal_links_target_id'), table_name='causal_links')
    op.drop_index(op.f('ix_causal_links_source_id'), table_name='causal_links')
    op.drop_index(op.f('ix_causal_links_relationship'), table_name='causal_links')
    op.drop_table('causal_links')
    op.drop_index(op.f('ix_attachments_owner_type'), table_name='attachments')
    op.drop_index(op.f('ix_attachments_owner_id'), table_name='attachments')
    op.drop_table('attachments')
    op.drop_index(op.f('ix_analysis_versions_component'), table_name='analysis_versions')
    op.drop_table('analysis_versions')
