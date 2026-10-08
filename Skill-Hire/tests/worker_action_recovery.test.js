const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('templates/worker.html', 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
async function run() {
  let timeout, cleared = 0, response;
  const apiStart = source.indexOf('  function api(');
  const context = {
    FormData, AbortController,
    setTimeout: (fn, ms) => { assert.equal(ms, 30000); timeout = fn; return 1; },
    clearTimeout: () => cleared++,
    fetch: async () => response
  };
  vm.runInNewContext(source.slice(apiStart, source.indexOf('\n  function ', apiStart + 10)), context);
  for (const text of ['<html>Unavailable</html>', '', 'null', '123', '"ok"']) {
    response = {ok: true, status: 200, text: async () => text};
    const result = await context.api('/test');
    assert.equal(result.ok, false); assert.equal(result.status, 200);
  }
  response = {ok: true, status: 200, text: async () => '[]'};
  assert.equal((await context.api('/test')).ok, true);
  response = {ok: false, status: 409, text: async () => '{"error":"Job already completed"}'};
  assert.equal((await context.api('/test')).status, 409);
  const options = {body: new FormData()};
  context.fetch = async (_, opts) => {
    assert.equal(opts.body, options.body); assert.equal(opts.credentials, 'same-origin');
    assert.equal(opts.headers, undefined); return response;
  };
  await context.api('/upload', options); assert.equal(options.signal, undefined);
  context.fetch = (_, opts) => new Promise((resolve, reject) => opts.signal.addEventListener('abort', () => reject({name: 'AbortError'})));
  const pending = context.api('/test'); timeout();
  const timedOut = await pending;
  assert.equal(timedOut.ok, false); assert.match(timedOut.data.error, /timed out/);
  assert.equal(cleared, 9);

  const start = source.indexOf('  var workerTimerRequests =');
  const timerCode = source.slice(start, source.indexOf('\n\n  var earningsPeriod', start));
  let resolveAction, resolveRefresh, writes = 0, refreshes = 0;
  const button = {disabled: false};
  const messages = [];
  const timerContext = {
    detailBookingId: 7, document: {querySelector: () => button}, alert: msg => messages.push(msg),
    api: () => { writes++; return new Promise(resolve => resolveAction = resolve); },
    loadBookings: () => { refreshes++; return new Promise(resolve => resolveRefresh = resolve); }
  };
  vm.runInNewContext(timerCode, timerContext);
  const action = timerContext.workTimer(7, 'start');
  timerContext.workTimer(7, 'start');
  assert.equal(writes, 1); assert.equal(button.disabled, true);
  resolveAction({ok: false, status: 0, data: {error: 'Timed out'}}); await tick();
  assert.equal(refreshes, 1); assert.equal(button.disabled, true);
  timerContext.workTimer(7, 'start'); assert.equal(writes, 1);
  resolveRefresh(true); await action;
  assert.equal(button.disabled, false); assert.match(messages[0], /Check the latest job status/);
  const completed = timerContext.workTimer(7, 'stop');
  resolveAction({ok: true, data: {actual_minutes: 60, work_amount: 100, total_amount: 100}}); await tick();
  resolveRefresh(true); await completed;
  assert.equal(refreshes, 2); assert.equal(button.disabled, false);
  assert(messages.some(msg => msg.includes('Work completed')));
  let submit, responseResolve, responseRefreshResolve, closed = 0, responseWrites = 0;
  const controls = {workerRespondSubmit: {}, workerRespondCancel: {}, workerRespondFeedback: {}, workerRejectReason: {value: ''}, workerRespondForm: {addEventListener: (_, fn) => submit = fn}};
  const responseContext = {
    document: {getElementById: id => controls[id]}, pendingWorkerResponse: {id: 7, action: 'accept'}, workerResponseBusy: false,
    wresponse: key => key, respondDialog: {close: () => closed++}, findBooking: () => ({id: 7, status: 'confirmed'}),
    api: () => {responseWrites++; return new Promise(resolve => responseResolve = resolve);},
    loadBookings: () => new Promise(resolve => responseRefreshResolve = resolve)
  };
  const responseStart = source.indexOf('  document.getElementById("workerRespondForm").addEventListener("submit"');
  vm.runInNewContext(source.slice(responseStart, source.indexOf('  function renderBookings()', responseStart)), responseContext);
  submit({preventDefault: () => {}}); submit({preventDefault: () => {}});
  assert.equal(responseWrites, 1); assert.equal(controls.workerRespondSubmit.disabled, true);
  responseResolve({ok: false, data: {error: 'Timed out'}}); await tick();
  assert.equal(responseContext.workerResponseBusy, true);
  responseRefreshResolve(true); await tick();
  assert.equal(closed, 1); assert.equal(responseContext.workerResponseBusy, false);
  console.log('Worker API timeout, unreadable response, upload and timer action recovery tests passed');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
