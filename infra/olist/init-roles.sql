-- psql reads mounted secrets; no executable bind-mount is required on macOS.
\set ON_ERROR_STOP on
\set writer_password `cat /run/secrets/writer_password`
\set reader_password `cat /run/secrets/reader_password`
SELECT format('CREATE ROLE olist_writer LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE',
              :'writer_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='olist_writer') \gexec
SELECT format('CREATE ROLE olist_reader LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE',
              :'reader_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='olist_reader') \gexec
REVOKE ALL ON DATABASE olist FROM PUBLIC;
GRANT CONNECT ON DATABASE olist TO olist_writer,olist_reader;
GRANT CREATE ON DATABASE olist TO olist_writer;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER ROLE olist_reader SET default_transaction_read_only = on;
ALTER ROLE olist_reader SET statement_timeout = '5s';
ALTER ROLE olist_writer SET statement_timeout = '120s';
ALTER ROLE olist_writer SET lock_timeout = '5s';
\unset writer_password
\unset reader_password
