// modules/prometheus/k6/step.js — owned by the module: ONE request.
// The framework owns how much load; this file owns how to ask.
// QUERY can come from K6_QUERY env var (preferred) or plan.json fallback.
import http from 'k6/http';

const PROM_URL = __ENV.PROM_URL || 'http://prometheus:9090';
const plan = JSON.parse(open('../../../plan.json'));
const QUERY = __ENV.K6_QUERY || plan.query;

export function step() {
  const res = http.get(
    `${PROM_URL}/api/v1/query?query=${encodeURIComponent(QUERY)}&time=${Math.floor(Date.now() / 1000)}`,
    { tags: { module: 'prometheus' } },
  );
  return res.status === 200;
}
