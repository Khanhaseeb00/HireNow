const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const source = fs.readFileSync('templates/dashboard.html', 'utf8');
const helpers = source.slice(source.indexOf('  function checkUnverifiedCheckout('), source.indexOf('  function payBooking('));
const tick = () => new Promise(resolve => setImmediate(resolve));
async function run() {
  for (const [variable, next, label] of [
    ['payFinalOnline', 'payOnline', 'Pay Online'],
    ['payOnline', 'payBalance', 'Pay Online Now'],
    ['payBalance', 'otpBtn', 'Pay Remaining Balance']
  ]) {
    const start = source.indexOf('        var ' + variable + '=');
    const end = source.indexOf('        var ' + next + '=', start);
    let click, options, failed, orderCalls = 0, syncCalls = 0, refreshes = 0;
    let syncOk = false;
    const alerts = [];
    const button = {dataset: {}, disabled: false, textContent: label, addEventListener: (_, fn) => click = fn};
    const context = {
      stackBody: {querySelector: () => button}, b: {id: 42},
      api: async path => {
        if (path.includes('/sync-payment')) { syncCalls++; return {ok: syncOk, data: {error: 'Sync unavailable'}}; }
        if (path.includes('/verify')) return {ok: false, data: {error: 'Verification timed out'}};
        orderCalls++; return {ok: true, data: {order_id: 'order_test', amount: 10000, currency: 'INR'}};
      },
      Razorpay: function (opts) { options = opts; this.on = (_, fn) => failed = fn; this.open = () => {}; },
      window: {alert: message => alerts.push(message)}, loadBookings: () => refreshes++, openBookingDetailsScreen: () => {}
    };
    vm.runInNewContext(helpers + source.slice(start, end), context);
    click(); await tick();
    assert.equal(button.disabled, true);
    options.modal.ondismiss();
    assert.equal(button.disabled, false); assert.equal(button.textContent, label);
    click(); await tick(); failed({error: {description: 'Declined'}});
    assert.equal(button.disabled, false); assert.equal(button.textContent, label);
    click(); await tick();
    options.handler({razorpay_order_id: 'order_test', razorpay_payment_id: 'pay_test', razorpay_signature: 'signature'});
    await tick();
    assert.equal(button.disabled, false); assert.equal(button.textContent, 'Check Payment Status');
    assert(alerts.some(message => message.includes('do not pay again')));
    options.modal.ondismiss(); assert.equal(button.textContent, 'Check Payment Status');
    const previousOrders = orderCalls;
    click(); await tick();
    assert.equal(syncCalls, 1); assert.equal(orderCalls, previousOrders);
    assert.equal(button.disabled, false); assert.equal(button.textContent, 'Check Payment Status');
    syncOk = true; click(); await tick();
    assert.equal(syncCalls, 2); assert.equal(orderCalls, previousOrders); assert.equal(refreshes, 1);
  }
  console.log('All three checkout flows: dismissal, failure, verification timeout and sync recovery passed');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
