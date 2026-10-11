# k6 with the SQL extension, for running the query load against PostgreSQL.
#
# Stock k6 has no Postgres driver, and the xk6-sql extension is what lets one
# script drive both systems: HTTP calls for Prometheus, sql.query for Postgres.
# The build needs network access to fetch the extension module.
FROM grafana/xk6:latest AS build
RUN xk6 build --with github.com/grafana/xk6-sql

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=build /home/xk6/k6 /usr/local/bin/k6
ENTRYPOINT ["k6"]
