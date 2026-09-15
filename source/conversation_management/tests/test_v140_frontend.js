// Targeted stream reducer tests: duplicate, unordered replay and generation changes.
const fs=require('fs');const vm=require('vm');const assert=require('assert/strict');
const path=require('path');const base=path.resolve(__dirname,'../app/web/chat');
const source=fs.readFileSync(path.join(base,'app.js'),'utf8');
assert.equal(source,fs.readFileSync(path.join(base,'chat-ui-v1.5.0.js'),'utf8'));
assert(fs.readFileSync(path.join(base,'index.html'),'utf8').includes('chat-ui-v1.5.0.js'));
assert.equal(fs.readFileSync(path.join(base,'styles.css'),'utf8'),fs.readFileSync(path.join(base,'chat-ui-v1.4.1.css'),'utf8'));
const state={currentTaskId:'task-a',currentAssistantId:'message-a',streamOutputs:new Map(),appliedContentEventIds:new Set()};
const message={id:'message-a',content:'old prompt',status:'PENDING'};
const context=vm.createContext({state,messageById:id=>id===message.id?message:null,renderMessages:()=>{},
  isFinalContentDelta:t=>['answer.delta','agent.output.delta'].includes(t),contentEventIdentity:(t,d,id)=>id});
function section(a,b){return source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));}
vm.runInContext(section('function outputFor(','function renderReasoning(')+
  section('function emptyOutput(','function acceptDiagnosisPause(')+
  section('function applyFinalContentDelta(','function taskStreamUrl('),context);
function output(type,sequence,content,generation='one',extra={}){context.applyOutputEvent(type,{generation_id:generation,sequence_no:sequence,message_id:message.id,content,...extra},`${sequence}-0`);}
function delta(sequence,content,generation='one'){context.applyFinalContentDelta('answer.delta',{generation_id:generation,sequence_no:sequence,message_id:message.id,content},`${sequence}-0`);}
output('answer.started',1);delta(3,'后');delta(2,'先');delta(3,'后');
assert.equal(message.content,'先后');output('answer.started',1);assert.equal(message.content,'先后');
output('answer.reasoning.delta',5,'后思考');output('answer.reasoning.delta',4,'先思考');output('answer.reasoning.delta',5,'后思考');
assert.equal(state.streamOutputs.get(message.id).reasoning,'先思考后思考');
output('answer.completed',6,'规范化完整回答','one',{replace:true});delta(3,'后');
assert.equal(message.content,'规范化完整回答');assert.equal(message.status,'COMPLETED');assert(state.streamOutputs.get(message.id).folded);
output('answer.started',9,'','two');output('answer.started',1);delta(8,'过期片段');
assert.equal(message.content,'');delta(10,'新回答','two');assert.equal(message.content,'新回答');
output('answer.failed',11,'','two');delta(10,'新回答','two');assert.equal(message.status,'FAILED');
console.log('8 frontend replay/build checks passed');
