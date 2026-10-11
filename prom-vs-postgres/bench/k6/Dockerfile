# k6 with the SQL extension, for running the query load against PostgreSQL.
#
# Stock k6 has no Postgres driver, and the xk6-sql extension is what lets one
# script drive both systems: HTTP calls for Prometheus, sql.query for Postgres.
# The build needs network access to fetch the extension module.
FROM grafana/xk6:latest AS build
# xk6 puts the binary in /xk6, not /home/xk6 -- the image's WORKDIR is /home/xk6
# but xk6 itself writes to /xk6/k6. COPY from the wrong path fails at build
# time with "not found", which looks like the build failed rather than a typo.
RUN xk6 build --with github.com/grafana/xk6-sql

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=build /xk6/k6 /usr/local/bin/k6
ENTRYPOINT ["k6"]
