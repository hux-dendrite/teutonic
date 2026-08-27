\set ON_ERROR_STOP on

BEGIN;

CREATE TABLE IF NOT EXISTS control_plane.evaluation_early_stopping_policies (
    competition_id uuid PRIMARY KEY
        REFERENCES control_plane.competitions(competition_id) ON DELETE RESTRICT,
    enabled boolean NOT NULL DEFAULT true,
    min_fraction double precision NOT NULL DEFAULT 0.4
        CHECK (min_fraction > 0 AND min_fraction <= 1),
    advantage_quantile double precision NOT NULL DEFAULT 0.95
        CHECK (advantage_quantile > 0 AND advantage_quantile <= 1),
    margin double precision NOT NULL DEFAULT 0.0 CHECK (margin >= 0),
    check_interval integer NOT NULL DEFAULT 100 CHECK (check_interval > 0),
    updated_at timestamp with time zone NOT NULL DEFAULT clock_timestamp()
);

ALTER TABLE control_plane.evaluation_early_stopping_policies
    OWNER TO teutonic_schema_owner;

INSERT INTO control_plane.evaluation_early_stopping_policies (competition_id)
SELECT competition_id
  FROM control_plane.competitions
ON CONFLICT (competition_id) DO NOTHING;

REVOKE ALL ON TABLE control_plane.evaluation_early_stopping_policies FROM PUBLIC;
GRANT SELECT ON TABLE control_plane.evaluation_early_stopping_policies TO teutonic_validator;
GRANT SELECT ON TABLE control_plane.evaluation_early_stopping_policies TO teutonic_auditor;

COMMIT;
