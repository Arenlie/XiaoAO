const fs=require('fs'),vm=require('vm'),assert=require('assert/strict'),path=require('path');
const source=fs.readFileSync(path.resolve(__dirname,'../app/web/chat/app.js'),'utf8');
function section(a,b){const i=source.indexOf(a);assert(i>=0,a);const j=source.indexOf(b,i);assert(j>i,b);return source.slice(i,j);}
const state={diagnosisConfirmation:{message_id:'M',expires_at:'2099-01-01',options:[{mode:'detailed',label:'旧按钮'}]}};
const context=vm.createContext({state,Date,escapeHtml:x=>String(x),formatTime:()=>'',pretty:JSON.stringify});
vm.runInContext(section('function statusText(','function messageById(')+
  section('function renderDiagnosisChoice(','function emptyOutput(')+
  section('function runningExecutionRow(','function renderExecutionEvent('),context);
const card=context.renderDiagnosisChoice({id:'M'});
assert(card.includes('回复')&&card.includes('快速分析')&&card.includes('详细诊断'));
assert(!card.includes('<button')&&!source.includes('data-diagnosis-mode')&&!source.includes('async function chooseDiagnosisMode('));
for(const status of ['CANCELLED','SKIPPED','COMPLETED','FAILED']){
  const start={event_type:'performance.span.started',span_id:'span-a'};
  const end={event_type:status==='FAILED'?'performance.span.failed':'performance.span.completed',span_id:'span-a',status};
  context.eventPayload=r=>r.payload||{};
  assert(!context.runningExecutionRow(start,[start,end]));
  assert(!/[A-Z_]/.test(context.statusText(status)));
}
const details=context.progressDetails({equip_no:'REAL_A_001',cache_hit:true,required_level:'point',
  task_id:'hidden',span_id:'hidden',reason:'builtin.supervisor.internal_call'});
assert.equal(details['设备编号'],'REAL_A_001');assert.equal(details['定位要求'],'测点');
assert.equal(details['复用已有结果'],'是');assert(!JSON.stringify(details).includes('hidden'));
assert(!JSON.stringify(details).includes('builtin'));
assert.equal(context.progressText('Asset MCP：实体检索'),'资产查询：设备、测点与区域检索');
assert.equal(context.progressText('快速识别查询对象'),'快速识别查询对象');
assert.equal(context.progressText('执行 query_points'),'设备测点查询');
assert.equal(context.progressText('phm-asset-mcp 接收并执行 MCP 工具 query_points'),'执行本项查询或分析。');
assert(source.includes('const previousChoice=state.diagnosisConfirmation'));
assert(source.includes('previousMessage.status="STOPPED"'));
// The renderer must produce an actual separator and bold heading, not escaped Markdown.
vm.runInContext(section('function sanitizeFinalAnswer(', 'function compactPreview(')+section('function renderMarkdown(', 'function saveLocalSettings('),context);
const rendered=context.renderMarkdown('正文。\n\n---\n\n**知识库依据：**\n\n[1] 维护手册');
assert(rendered.includes('<hr>')&&rendered.includes('<strong>知识库依据：</strong>'));
console.log('14 customer-flow frontend checks passed');
