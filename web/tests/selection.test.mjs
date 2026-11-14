import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createSelectionGate, commitIfCurrent, documentUsage} from '../lib/selection.mjs';
function deferred() {let resolve;const promise=new Promise(r=>{resolve=r;});return {promise,resolve};}
test('completed-run refresh cannot overwrite another selected conversation', async () => {
  const gate=createSelectionGate(), first=gate.begin('first'), response=deferred();
  let visible={id:'first'};
  const pending=commitIfCurrent(gate,first,()=>response.promise,value=>{visible=value;});
  gate.begin('second'); visible={id:'second'}; response.resolve({id:'first',messages:['late']});
  assert.equal(await pending,null); assert.deepEqual(visible,{id:'second'});
});
test('reopening the same conversation invalidates its older upload/detail request', async () => {
  const gate=createSelectionGate(), older=gate.begin('same'), response=deferred();
  let visible;
  const pending=commitIfCurrent(gate,older,()=>response.promise,value=>{visible=value;});
  const newer=gate.begin('same');
  await commitIfCurrent(gate,newer,async()=>({version:2}),value=>{visible=value;});
  response.resolve({version:1}); await pending;
  assert.deepEqual(visible,{version:2});
});
test('current detail commits, deselection invalidates all outstanding requests', async () => {
  const gate=createSelectionGate(), ticket=gate.begin('a'); let committed=false;
  assert.deepEqual(await commitIfCurrent(gate,ticket,async()=>({id:'a'}),()=>{committed=true;}),{id:'a'});
  assert.equal(committed,true); gate.begin(null); assert.equal(gate.isCurrent(ticket),false);
});
test('document usage merges replay and durable run snapshots without duplicates', () => {
  assert.deepEqual(documentUsage(['a'],['a','b']),['a','b']);
  assert.deepEqual(documentUsage(undefined,['b']),['b']);
  assert.deepEqual(documentUsage(['a'],[null,{},'c']),['a','c']);
});
