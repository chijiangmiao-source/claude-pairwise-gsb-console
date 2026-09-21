import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const app = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const code = app.slice(app.indexOf('async function resetPairRetries('), app.indexOf('function pairButtons('));
function fixture(confirm = true) {
  const calls = []; let open = true;
  const c = vm.createContext({
    api: async (path, options) => {calls.push([path, options.method]); return {operationId:'queue-op'};},
    render: async () => {open = false;},
    showPair: async id => {open = true; calls.push(['shown',id]);},
    notify: () => {}, window:{confirm: () => confirm},
    poll: async (id, done) => {assert.equal(id,'queue-op'); await done();},
  });
  vm.runInContext(code,c); return {c,calls,isOpen:()=>open};
}
test('reset refreshes list before reopening updated pair detail', async()=>{
  const f=fixture(); await f.c.resetPairRetries('pair-test',{});
  assert.equal(f.isOpen(),true);
  assert.deepEqual(f.calls,[['/api/pairs/pair-test/reset-retries','POST'],['shown','pair-test']]);
});
test('queue uses only selected side and keeps updated detail open', async()=>{
  const f=fixture(); await f.c.queuePairArm('pair-test','B',{});
  assert.equal(f.isOpen(),true);
  assert.deepEqual(f.calls,[['/api/pairs/pair-test/arms/B/queue','POST'],['shown','pair-test']]);
});
test('declining requeue leaves the project untouched', async()=>{
  const f=fixture(false); await f.c.queuePairArm('pair-test','A',{});
  assert.deepEqual(f.calls,[]);
});
