// load/ramp.js — owned by the framework, written once, never regenerated.
//
// The ONLY thing Python generates is plan.json (stages, executor,
// thresholds, query). This file reads it at init() and never changes.
// The module owns exactly one step: ONE request, imported below.

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend } from 'k6/metrics';
import exec from 'k6/execution';

// plan.json is generated per run by the driver from the scenario YAML.
const plan = JSON.parse(open('../plan.json'));

// Module step: one request against the system under test.
// The module knows HOW to ask; the framework knows HOW MUCH to ask.
// The driver symlinks modules/active -> the current module before the run,
// so this import never names a module and no JS is ever generated.
import { step } from '../modules/active/k6/step.js';

const latency = new Trend('step_latency_ms', true);

// maxVUs bounds client-side concurrency. It is a resource-envelope decision
// (benchctl.yaml k6_max_vus), not a module's business. A module may lower it
// via k6.max_vus. Framework caps preAllocatedVUs to not exceed maxVUs.
const maxVUs = plan.load.max_vus ?? 500;
const preAllocatedVUs = Math.min(50, maxVUs);

export const options = {
  scenarios: {
    // Two executor families:
    //  - arrival-rate (the metrics scenarios): the framework paces at a
    //    target rate and measures latency against it.
    //  - constant-vus (the userdb pool models): a CLOSED loop. VUs push
    //    registrations back to back, so throughput = VUs / latency and
    //    the VU budget (= P x conns, the app's pool sizing) is the thing
    //    saturating. Pool saturation is the measurement here.
    ramp: plan.load.executor === 'constant_vus'
      ? {
          executor: 'constant-vus',
          vus: maxVUs,
          duration: plan.load.duration || '90s',
        }
      : {
          executor: plan.load.executor,
          startRate: plan.load.start_rate,
          timeUnit: plan.load.time_unit,
          stages: plan.load.stages.map((s) => ({ target: s.target, duration: s.duration })),
          preAllocatedVUs: preAllocatedVUs,
          maxVUs: maxVUs,
        },
  },
  thresholds: {
    // k6 aborts with exit 99 when a threshold breaks: that is the
    // "it broke HERE" signal, not an error.
    step_latency_ms: [`p(95)<${plan.thresholds.p95_ms}`],
    dropped_iterations: ['count<1'],
  },
};

export default function () {
  const t0 = Date.now();
  const ok = step();
  latency.add(Date.now() - t0);
  check(ok, { 'step ok': (v) => v === true });
}

export function handleSummary(data) {
  // One summary file per leg so each leg's k6 numbers survive instead of
  // being overwritten by the next leg. plan.system is like "postgres/jsonb";
  // slug it to "postgres-jsonb" for a safe filename. When the scenario has a
  // filesystem dimension, plan.fs is appended — the same rule as
  // shared/leg.py, which is what the verifier matches against. plan.extra
  // carries whatever else the leg varies (the number of shards).
  const slug = (plan.system || 'system').replace(/\//g, '-')
    + (plan.fs ? '-' + plan.fs : '')
    + (plan.extra ? '-' + plan.extra : '');
  return { [`/results/summary-${slug}.json`]: JSON.stringify(data) };
}
