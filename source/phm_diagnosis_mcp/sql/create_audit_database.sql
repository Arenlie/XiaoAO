-- Run as a PostgreSQL superuser on a new server.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'phm_diagnosis_mcp') THEN
        CREATE ROLE phm_diagnosis_mcp LOGIN PASSWORD '123456';
    END IF;
END $$;

SELECT 'CREATE DATABASE phm_diagnosis_mcp OWNER phm_diagnosis_mcp'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'phm_diagnosis_mcp')\gexec
