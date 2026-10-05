# One image for both jobs: ingesting the dataset and running the query suite.
#
# They share this because the query bench needs psycopg (to talk to Postgres)
# and the generator needs numpy (to build the samples). Splitting them would
# mean a second build for a few megabytes of difference, and one image means
# one thing to rebuild when the dataset changes.
# Bumped with --build-arg PYTHON_VERSION=3.13 rather than tracking :latest.
# This image is tooling, not a system under test: it never appears in a
# measured number, and a :latest tag that outruns the numpy/psycopg/cramjam
# wheels turns a version bump into a source build. The two images that DO
# decide the comparison are on :latest.
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

RUN pip install --no-cache-dir \
      numpy \
      psycopg[binary] \
      cramjam

WORKDIR /work
COPY generator/ /work/generator/
COPY bench/queries.py /work/bench/queries.py
COPY bench/bench.py /work/bench/bench.py
COPY bench/parity.py /work/bench/parity.py

ENV PYTHONPATH=/work/generator:/work/bench
