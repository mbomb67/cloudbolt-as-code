#!/bin/bash
PGPASSWORD='{{ server.postgres_database_password }}' psql -h {{ server.hostname }} \
     -p {{server.postgres_port}} \
     -U {{ server.postgres_database_owner }} \
     -d {{ server.postgres_database_name }} \
     -v ON_ERROR_STOP=1 --no-psqlrc <<'CB_SQL_END'
{{ SQLCMD }}
CB_SQL_END