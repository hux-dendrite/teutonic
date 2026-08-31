\set ON_ERROR_STOP on

BEGIN;

CREATE OR REPLACE VIEW control_plane.dashboard_dataset_versions
WITH (security_barrier='true') AS
 SELECT competition.netuid,
    competition.chain_generation,
    competition.name AS competition,
    config.config_version,
    config.dataset_label,
    config.eval_n,
    config.delta_threshold,
    config.created_at AS config_created_at,
    manifest."position",
    manifest.name,
    manifest.manifest_url,
    manifest.manifest_sha256,
    manifest.sample_proportion,
    manifest.manifest_json
   FROM ((control_plane.competitions competition
     JOIN control_plane.evaluation_configs config ON (config.competition_id = competition.competition_id))
     JOIN control_plane.dataset_manifests manifest ON (manifest.evaluation_config_id = config.evaluation_config_id));

ALTER VIEW control_plane.dashboard_dataset_versions OWNER TO teutonic_schema_owner;
GRANT SELECT ON TABLE control_plane.dashboard_dataset_versions TO teutonic_dashboard_view;

COMMIT;
