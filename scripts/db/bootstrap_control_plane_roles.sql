DO $roles$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        'teutonic_schema_owner',
        'teutonic_access_controller',
        'teutonic_validator',
        'teutonic_weight_publisher',
        'teutonic_dashboard_view',
        'teutonic_auditor'
    ]
    LOOP
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format(
                'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT',
                role_name
            );
        END IF;
    END LOOP;
END
$roles$;
