const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const source = fs.readFileSync('templates/dashboard.html', 'utf8');
const start = source.indexOf('var payOnline=stackBody.querySelector("#refPayOnline")');
const end = source.indexOf('var payBalance=', start);
const code = source.slice(start, end);
async function run() {
  let click, options, failed;
  const button = {disabled: false, textContent: 'Pay Online Now', addEventListener: (_, fn) => click = fn};
  const context = {
    stackBody: {querySelector: () => button}, b: {id: 42},
    api: async () => ({ok: true, data: {order_id: 'order_test', amount: 10000, currency: 'INR'}}),
    Razorpay: function (opts) { options = opts; this.on = (_, fn) => failed = fn; this.open = () => {}; },
    window: {alert: () => {}}, loadBookings: () => {}, openBookingDetailsScreen: () => {}
  };
  vm.runInNewContext(code, context);
  click(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(button.disabled, true);
  options.modal.ondismiss();
  assert.equal(button.disabled, false); assert.equal(button.textContent, 'Pay Online Now');
  click(); await new Promise(resolve => setImmediate(resolve));
  failed({error: {description: 'Declined'}});
  assert.equal(button.disabled, false); assert.equal(button.textContent, 'Pay Online Now');
  context.api = async () => ({ok: false, data: {error: 'Checkout timed out'}});
  click(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(button.disabled, false); assert.equal(button.textContent, 'Pay Online Now');
  console.log('Checkout dismissal, failure and provider-error recovery tests passed');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
