const fs=require('fs'),vm=require('vm'),assert=require('assert/strict'),path=require('path');
const source=fs.readFileSync(path.resolve(__dirname,'../app/web/chat/app.js'),'utf8');
function section(a,b){const i=source.indexOf(a);assert(i>=0,a);const j=source.indexOf(b,i);assert(j>i,b);return source.slice(i,j);}
let aborted=0;
const message={id:'M',content:'',status:'PENDING'};
const state={currentAssistantId:'M',currentTaskId:'T',settledDiagnosisIds:new Set(),streamOutputs:new Map(),appliedContentEventIds:new Set(),isGenerating:true,streamAbort:{abort(){aborted++;}}};
const context=vm.createContext({state,messageById:id=>id==='M'?message:null,renderMessages(){},updateGeneratingUi(){},
    isFinalContentDelta:t=>['answer.delta','agent.output.delta'].includes(t),contentEventIdentity:(t,d,id)=>id});
vm.runInContext(section('function outputFor(','function renderReasoning(')+
    section('function emptyOutput(','function acceptDiagnosisPause(')+
    section('function acceptDiagnosisPause(','async function recoverDiagnosisChoice(')+
    section('function applyFinalContentDelta(','function taskStreamUrl('),context);
const common={task_id:'T',message_id:'M',generation_id:'G',output_kind:'confirmation_prompt',channel:'final'};
context.applyOutputEvent('answer.started',{...common,sequence_no:1},'1-0');
context.applyFinalContentDelta('answer.delta',{...common,sequence_no:2,status:'COMPLETED',content:'可以回复快速分析或详细诊断。'},'2-0');
assert.equal(message.content,'可以回复快速分析或详细诊断。');
context.acceptDiagnosisPause({confirmation_id:'C',message_id:'M',status:'COMPLETED',stream_continues:false,question:message.content});
assert.equal(state.isGenerating,false);assert.equal(message.status,'COMPLETED');assert.equal(aborted,0);
context.applyOutputEvent('answer.completed',{...common,sequence_no:4,content:message.content,replace:true},'4-0');
assert.equal(message.status,'COMPLETED');
context.applyFinalContentDelta('answer.delta',{...common,sequence_no:2,content:'可以回复快速分析或详细诊断。'},'2-0');
assert.equal(message.content,'可以回复快速分析或详细诊断。');
assert(source.includes('previousMessage.status!=="COMPLETED"'));
assert(!source.includes('data-diagnosis-mode'));
console.log('8 diagnosis prompt completion frontend checks passed');
