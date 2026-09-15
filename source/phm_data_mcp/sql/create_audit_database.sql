-- Run with psql as a PostgreSQL administrator.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'phm_data_mcp') THEN
        CREATE ROLE phm_data_mcp LOGIN PASSWORD '123456';
    ELSE
        ALTER ROLE phm_data_mcp LOGIN PASSWORD '123456';
    END IF;
END
$$;

SELECT 'CREATE DATABASE phm_data_mcp OWNER phm_data_mcp'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'phm_data_mcp')
\gexec

ALTER DATABASE phm_data_mcp SET timezone TO 'Asia/Shanghai';
