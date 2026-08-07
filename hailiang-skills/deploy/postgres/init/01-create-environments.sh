#!/usr/bin/env bash
set -euo pipefail
psql --username "$POSTGRES_USER" --dbname postgres --set ON_ERROR_STOP=1 \
  --set prod_password="$HAILIANG_PROD_DB_PASSWORD" \
  --set test_password="$HAILIANG_TEST_DB_PASSWORD" <<'SQL'
CREATE ROLE hailiang_prod LOGIN PASSWORD :'prod_password';
CREATE ROLE hailiang_test LOGIN PASSWORD :'test_password';
CREATE DATABASE hailiang_skills OWNER hailiang_prod;
CREATE DATABASE hailiang_skills_test OWNER hailiang_test;
SQL
