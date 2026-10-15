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
    ramp: {
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
  return { '/results/summary.json': JSON.stringify(data) };
}
