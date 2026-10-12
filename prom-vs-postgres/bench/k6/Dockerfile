# k6 with SQL support, for running the query load against PostgreSQL.
#
# Stock k6 has no database support. xk6-sql is the API (sql.open / sql.query),
# but it ships NO drivers: each database is a separate module. Without the
# driver module the build succeeds, k6 starts, and every sql.open fails at
# runtime with "unsupported database: invalid driver parameter type" -- a
# message about the *parameter*, which sends you looking at the DSN instead of
# at the fact that no driver for that name is registered.
#
# So: xk6-sql for the interface, xk6-sql-driver-postgres for Postgres.
#
# The build needs network access to fetch both modules.
FROM grafana/xk6:latest AS build
# xk6 puts the binary in /xk6, not /home/xk6 -- the image's WORKDIR is /home/xk6
# but xk6 itself writes to /xk6/k6. COPY from the wrong path fails at build
# time with "not found", which looks like the build failed rather than a typo.
RUN xk6 build \
      --with github.com/grafana/xk6-sql \
      --with github.com/grafana/xk6-sql-driver-postgres

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=build /xk6/k6 /usr/local/bin/k6
ENTRYPOINT ["k6"]
