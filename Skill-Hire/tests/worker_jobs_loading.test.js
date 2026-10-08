const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('templates/worker.html', 'utf8');
const start = source.indexOf('  var workerJobsRequest=');
const end = source.indexOf('\n  var WORKER_RESPONSE_UI', start);
async function run() {
  let retry, renders = 0;
  const pending = [];
  const elements = {
    workerBookingLoadStatus: {}, workerBookingLoadMessage: {},
    workerBookingRetry: {addEventListener: (_, callback) => retry = callback},
    activeJobCount: {}, requestCountText: {}
  };
  const old = [{id: 1, status: 'requested'}];
  const context = {
    state: {worker: {id: 10}, bookings: old},
    document: {getElementById: id => elements[id]},
    api: () => new Promise(resolve => pending.push(resolve)),
    renderBookings: () => renders++, renderProfile: () => {}, applyWorkerJobLabels: () => {},
    renderEarningsScreen: () => {}, renderMessagesScreen: () => {},
    currentWorkerPanel: 'work', detailBookingId: null
  };
  vm.runInNewContext(source.slice(start, end), context);
  const failed = context.loadBookings();
  assert.equal(elements.workerBookingRetry.hidden, true);
  pending.shift()({ok: false, data: {error: '<script>network error</script>'}});
  assert.equal(await failed, false);
  assert.equal(context.state.bookings, old);
  assert.equal(elements.workerBookingLoadStatus.hidden, false);
  assert.equal(elements.workerBookingRetry.disabled, false);
  assert.match(elements.workerBookingLoadMessage.textContent, /Last loaded jobs may be outdated/);
  assert.match(elements.workerBookingLoadMessage.textContent, /<script>/); // textContent, never HTML
  retry();
  pending.shift()({ok: true, data: []});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(context.state.bookings.length, 0);
  assert.equal(elements.workerBookingLoadStatus.hidden, true);
  const older = context.loadBookings(), latest = context.loadBookings();
  const olderResolve = pending.shift(), latestResolve = pending.shift();
  latestResolve({ok: true, data: [{id: 2, status: 'completed'}]}); await latest;
  olderResolve({ok: true, data: old}); assert.equal(await older, false);
  assert.equal(context.state.bookings[0].status, 'completed');
  const beforeSession = renders;
  const previousSession = context.loadBookings();
  context.workerJobsSession++;
  pending.shift()({ok: true, data: old});
  assert.equal(await previousSession, false); assert.equal(renders, beforeSession);
  const previousWorker = context.loadBookings();
  context.state.worker = {id: 11};
  pending.shift()({ok: true, data: old});
  assert.equal(await previousWorker, false); assert.equal(renders, beforeSession);
  const loggedOut = context.loadBookings();
  context.state.worker = null;
  pending.shift()({ok: true, data: old});
  assert.equal(await loggedOut, false); assert.equal(await context.loadBookings(), false);
  console.log('Worker jobs: visible errors, safe retry, stale-response and session isolation tests passed');
}
run().catch(error => {console.error(error); process.exitCode = 1;});
