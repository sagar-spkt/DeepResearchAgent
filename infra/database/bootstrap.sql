-- Run once using the RDS administrator through an authenticated in-VPC job.
-- Supply role passwords via psql variables from Secrets Manager; never log them.
-- psql --set ON_ERROR_STOP=1 --set app_password=... --set langfuse_password=...
CREATE ROLE research_app LOGIN PASSWORD :'app_password';
CREATE ROLE langfuse_app LOGIN PASSWORD :'langfuse_password';
ALTER DATABASE research OWNER TO research_app;
CREATE DATABASE langfuse OWNER langfuse_app;
\connect research
CREATE EXTENSION IF NOT EXISTS vector;
GRANT USAGE, CREATE ON SCHEMA public TO research_app;
-- Application migrations run as research_app; Langfuse migrations use langfuse_app.
