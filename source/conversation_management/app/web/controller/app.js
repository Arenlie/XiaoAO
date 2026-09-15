"use strict";

const $ = (id) => document.getElementById(id);
const STORAGE = {
  api: "conversationControllerApiBase",
  user: "conversationControllerUserToken",
  data: "conversationControllerDataToken",
  controller: "conversationControllerAdminToken",
  auto: "conversationControllerAutoRefresh",
};

const GROUPS = [
  { id: "health", title: "健康与文档", description: "存活、就绪、指标和 OpenAPI" },
  { id: "diagnostics", title: "生产诊断", description: "任务、Outbox、Redis、RLS 与 Trace" },
  { id: "conversations", title: "会话管理", description: "创建、列表、详情、标题、置顶、删除与分支" },
  { id: "generation", title: "生成任务", description: "发送消息、任务状态、SSE、停止和完整执行历史" },
  { id: "versions", title: "消息版本", description: "编辑重提、重新生成和替代版本" },
  { id: "profile", title: "用户画像", description: "画像读写与常用问题推荐" },
  { id: "agents", title: "智能体与工具", description: "Agent Registry 开关、健康状态和 Tool Registry" },
];

const ENDPOINTS = [
  { id: "health", group: "health", title: "轻量存活检查", method: "GET", path: "/health", description: "确认 FastAPI 进程存活。", auth: false },
  { id: "ready", group: "health", title: "依赖就绪检查", method: "GET", path: "/ready", description: "检查 PostgreSQL 和 Redis。", auth: false },
  { id: "metrics", group: "health", title: "Prometheus 指标", method: "GET", path: "/metrics", description: "读取 TTFT、Outbox 和任务指标。", auth: false, text: true },
  { id: "openapi", group: "health", title: "OpenAPI 定义", method: "GET", path: "/openapi.json", description: "读取后端实际接口定义。", auth: false },

  { id: "diag-overview", group: "diagnostics", title: "租户运行总览", method: "GET", path: "/chat/v1/diagnostics/overview", description: "当前用户的会话、任务、Outbox、Redis Stream 和 RLS 状态。" },
  { id: "diag-runtime", group: "diagnostics", title: "运行配置摘要", method: "GET", path: "/chat/v1/diagnostics/runtime", description: "脱敏展示实体 Workflow、Agent、SSE、并发、Outbox、RLS 与 Trace 配置。" },
  { id: "diag-tasks", group: "diagnostics", title: "最近生成任务", method: "GET", path: "/chat/v1/diagnostics/tasks", description: "按状态查看当前用户最近任务。", query: { status: "", limit: "$limit" } },
  { id: "diag-outbox", group: "diagnostics", title: "最近 Outbox 事件", method: "GET", path: "/chat/v1/diagnostics/outbox", description: "查看可靠投递状态、重试次数和错误。", query: { status: "", limit: "$limit" } },
  { id: "diag-events", group: "diagnostics", title: "持久化执行事件", method: "GET", path: "/chat/v1/diagnostics/tasks/{task_id}/execution-events", description: "查看任务持久化的 Graph、智能体、工具、文件处理和纠错事件。", pathParams: { task_id: "$task_id" } },

  { id: "conv-create", group: "conversations", title: "创建空会话", method: "POST", path: "/chat/v1/conversations", description: "创建会话和根分支。", body: { app_code: "xiaoao" } },
  { id: "conv-list", group: "conversations", title: "会话列表", method: "GET", path: "/chat/v1/conversations", description: "按标题搜索并使用 URL 游标分页读取会话。", query: { app_code: "xiaoao", search: "", limit: "$limit", "X-Next-Cursor": "" } },
  { id: "conv-get", group: "conversations", title: "会话详情", method: "GET", path: "/chat/v1/conversations/{conversation_id}", description: "返回当前活动分支和消息。", pathParams: { conversation_id: "$conversation_id" } },
  { id: "conv-rename", group: "conversations", title: "重命名会话", method: "PATCH", path: "/chat/v1/conversations/{conversation_id}", description: "设置手动标题，后续自动标题不再覆盖。", pathParams: { conversation_id: "$conversation_id" }, body: { title: "酸轧车间设备健康度" } },
  { id: "conv-pin", group: "conversations", title: "置顶会话", method: "POST", path: "/chat/v1/conversations/{conversation_id}/pin", description: "将会话置顶。", pathParams: { conversation_id: "$conversation_id" } },
  { id: "conv-unpin", group: "conversations", title: "取消置顶", method: "DELETE", path: "/chat/v1/conversations/{conversation_id}/pin", description: "取消会话置顶。", pathParams: { conversation_id: "$conversation_id" } },
  { id: "conv-messages", group: "conversations", title: "活动分支消息", method: "GET", path: "/chat/v1/conversations/{conversation_id}/messages", description: "读取当前活动分支消息和替代版本索引。", pathParams: { conversation_id: "$conversation_id" } },
  { id: "conv-branches", group: "conversations", title: "会话分支列表", method: "GET", path: "/chat/v1/conversations/{conversation_id}/branches", description: "查看 ROOT、EDIT 和 REGENERATE 分支。", pathParams: { conversation_id: "$conversation_id" } },
  { id: "conv-activate", group: "conversations", title: "激活分支", method: "POST", path: "/chat/v1/conversations/{conversation_id}/branches/{branch_id}/activate", description: "切换当前活动分支。", pathParams: { conversation_id: "$conversation_id", branch_id: "$branch_id" } },
  { id: "conv-delete", group: "conversations", title: "删除会话", method: "DELETE", path: "/chat/v1/conversations/{conversation_id}", description: "停止运行任务并软删除会话。", pathParams: { conversation_id: "$conversation_id" }, danger: true },

  { id: "chat-send", group: "generation", title: "发送消息", method: "POST", path: "/chat/v1/chat/messages", description: "创建消息树节点、任务和 Outbox，返回 SSE 地址。", dataAuth: true, idempotency: true, body: { conversation_id: null, app_code: "xiaoao", content: "查看酸轧车间设备健康度", attachments: [], execution_mode: "normal" } },
  { id: "task-get", group: "generation", title: "查询任务", method: "GET", path: "/chat/v1/tasks/{task_id}", description: "查看任务状态和错误。", pathParams: { task_id: "$task_id" } },
  { id: "task-events", group: "generation", title: "订阅 SSE", method: "GET", path: "/chat/v1/tasks/{task_id}/events", description: "实时显示 Graph、主智能体、子智能体、工具、文件处理和回答事件。", pathParams: { task_id: "$task_id" }, sse: true },
  { id: "task-stop", group: "generation", title: "停止生成", method: "POST", path: "/chat/v1/tasks/{task_id}/stop", description: "设置停止标志并调用 Dify stop。", pathParams: { task_id: "$task_id" }, danger: true },

  { id: "msg-edit", group: "versions", title: "编辑并重新提交", method: "POST", path: "/chat/v1/messages/{user_message_id}/edit-and-resubmit", description: "保留原路径并创建 EDIT 分支。", pathParams: { user_message_id: "$user_message_id" }, dataAuth: true, body: { content: "查看酸轧车间最近一周设备健康度", attachments: null, execution_mode: "normal" } },
  { id: "msg-regenerate", group: "versions", title: "重新生成回答", method: "POST", path: "/chat/v1/messages/{assistant_message_id}/regenerate", description: "保留原回答并创建 REGENERATE 分支。", pathParams: { assistant_message_id: "$assistant_message_id" }, dataAuth: true, body: { force_reresolve: false, execution_mode: "normal" } },
  { id: "msg-alternatives", group: "versions", title: "消息替代版本", method: "GET", path: "/chat/v1/messages/{message_id}/alternatives", description: "查看同父节点的用户问题或助手回答版本。", pathParams: { message_id: "$message_id" } },
  { id: "msg-execution-events", group: "versions", title: "消息执行历史", method: "GET", path: "/chat/v1/messages/{message_id}/execution-events", description: "查看已持久化的 Graph、智能体、工具、文件处理、重试和结果事件。", pathParams: { message_id: "$message_id" } },

  { id: "profile-get", group: "profile", title: "读取用户画像", method: "GET", path: "/chat/v1/profile", description: "读取跨会话共享画像。" },
  { id: "profile-update", group: "profile", title: "更新用户画像", method: "PATCH", path: "/chat/v1/profile", description: "设置负责区域、设备和回答偏好。", body: { profile_json: { job_role: "故障诊断工程师", responsible_areas: ["酸轧车间"], responsible_equipment: [], preferred_topics: ["健康度", "趋势", "报警"] } } },
  { id: "profile-delete", group: "profile", title: "删除用户画像", method: "DELETE", path: "/chat/v1/profile", description: "清空当前用户画像。", danger: true },
  { id: "suggested", group: "profile", title: "常用问题推荐", method: "GET", path: "/chat/v1/suggested-queries", description: "根据历史查询统计返回建议问题。", query: { app_code: "xiaoao", limit: 10 } },
  { id: "agents-list", group: "agents", title: "智能体列表", method: "GET", path: "/chat/v1/controller/agents", description: "读取当前注册智能体、开关、准入和健康状态。", controllerAuth: true },
  { id: "agents-get", group: "agents", title: "智能体详情", method: "GET", path: "/chat/v1/controller/agents/{agent_id}", description: "查看单个智能体完整描述。", pathParams: { agent_id: "$agent_id" }, controllerAuth: true },
  { id: "agents-update", group: "agents", title: "更新智能体开关", method: "PATCH", path: "/chat/v1/controller/agents/{agent_id}", description: "分别控制 enabled、routing_enabled 和 execution_enabled。", pathParams: { agent_id: "$agent_id" }, controllerAuth: true, body: { enabled: true, routing_enabled: true, execution_enabled: true, maintenance_message: null } },
  { id: "agents-enable", group: "agents", title: "启用智能体", method: "POST", path: "/chat/v1/controller/agents/{agent_id}/enable", description: "同时开启主开关、路由和执行。", pathParams: { agent_id: "$agent_id" }, controllerAuth: true },
  { id: "agents-disable", group: "agents", title: "关闭智能体", method: "POST", path: "/chat/v1/controller/agents/{agent_id}/disable", description: "同时关闭主开关、路由和执行；核心通用内容分析智能体应保持启用。", pathParams: { agent_id: "$agent_id" }, controllerAuth: true, danger: true },
  { id: "agents-health", group: "agents", title: "智能体健康检查", method: "POST", path: "/chat/v1/controller/agents/{agent_id}/health-check", description: "执行适配器健康检查并持久化最近状态。", pathParams: { agent_id: "$agent_id" }, controllerAuth: true },
  { id: "tools-list", group: "agents", title: "工具注册表", method: "GET", path: "/chat/v1/controller/tools", description: "查看 HTTP、Dify Workflow、本地和 MCP 工具。", controllerAuth: true },
];

const state = { autoTimer: null, results: new Map(), sseAbort: new Map() };

function apiBase() { return ($("apiBase").value.trim() || window.location.origin).replace(/\/+$/, ""); }
function userToken() { return $("userToken").value.trim(); }
function dataToken() { return $("dataToken").value.trim(); }
function controllerToken() { return $("controllerToken").value.trim(); }
function pretty(value) { return typeof value === "string" ? value : JSON.stringify(value, null, 2); }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c])); }
function safeId(value) { return String(value).replace(/[^a-zA-Z0-9_-]/g, "-"); }
function clone(value) { return value == null ? value : JSON.parse(JSON.stringify(value)); }
function uuid() { return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`; }
function formatDuration(ms) { return Number.isFinite(ms) ? `${Math.round(ms)} ms` : "-"; }
function nowTime() { return new Date().toLocaleTimeString(); }
function endpointById(id) { return ENDPOINTS.find(e => e.id === id); }

function toast(message, type="info", timeout=3000) {
  const item = document.createElement("div"); item.className = `toast ${type}`; item.textContent = message;
  $("toastContainer").appendChild(item); setTimeout(() => item.remove(), timeout);
}

async function copyText(text, message="已复制") {
  try { await navigator.clipboard.writeText(text); } catch (_) {
    const area = document.createElement("textarea"); area.value = text; document.body.appendChild(area); area.select(); document.execCommand("copy"); area.remove();
  }
  toast(message, "success");
}

function contextValues() {
  return {
    conversation_id: $("ctxConversationId").value.trim(), task_id: $("ctxTaskId").value.trim(),
    user_message_id: $("ctxUserMessageId").value.trim(), assistant_message_id: $("ctxAssistantMessageId").value.trim(),
    message_id: $("ctxMessageId").value.trim(), branch_id: $("ctxBranchId").value.trim(),
    agent_id: $("ctxAgentId").value.trim(), limit: Number($("ctxLimit").value || 30),
  };
}

function resolveTokens(value, ctx=contextValues()) {
  if (Array.isArray(value)) return value.map(v => resolveTokens(v, ctx));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([k,v]) => [k, resolveTokens(v, ctx)]));
  if (typeof value === "string" && value.startsWith("$")) return ctx[value.slice(1)] ?? value;
  return value;
}

function cleanQuery(query) {
  return Object.fromEntries(Object.entries(query || {}).filter(([,v]) => v !== "" && v !== null && v !== undefined));
}

function buildUrl(path, query={}) {
  const url = new URL(apiBase() + path);
  Object.entries(cleanQuery(query)).forEach(([k,v]) => url.searchParams.set(k, String(v)));
  if (userToken() && url.pathname.startsWith("/chat/v1/")) {
    url.searchParams.set("X-User-Token", userToken());
  }
  return url.toString();
}

function substitutePath(path, params={}) {
  let result = path;
  Object.entries(params).forEach(([k,v]) => result = result.replace(`{${k}}`, encodeURIComponent(String(v ?? ""))));
  return result;
}

function parseJson(text, fallback={}) { const raw = String(text || "").trim(); return raw ? JSON.parse(raw) : fallback; }

function requestHeaders(endpoint, accept="application/json") {
  const headers = { Accept: accept };
  if (endpoint.dataAuth) headers["Token"] = dataToken();
  if (endpoint.controllerAuth) headers["Controller-Token"] = controllerToken();
  if (endpoint.idempotency) headers["Idempotency-Key"] = uuid();
  return headers;
}

async function sendHttp(endpoint, { path, query, body }) {
  const url = buildUrl(path, query); const headers = requestHeaders(endpoint);
  const options = { method: endpoint.method, headers };
  if (!["GET","HEAD"].includes(endpoint.method) && body !== null && body !== undefined) {
    headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body);
  }
  const started = performance.now();
  try {
    const response = await fetch(url, options); const raw = await response.text(); let data = raw;
    if (!endpoint.text && raw) { try { data = JSON.parse(raw); } catch (_) {} }
    if (!raw) data = null;
    return { ok: response.ok, status: response.status, statusText: response.statusText, durationMs: performance.now()-started, url, method:endpoint.method, data, headers:Object.fromEntries(response.headers.entries()) };
  } catch (error) {
    return { ok:false, networkError:true, status:0, statusText:"NETWORK_ERROR", durationMs:performance.now()-started, url, method:endpoint.method, error:String(error), data:null };
  }
}

function endpointInput(endpoint) {
  const pathParams = {};
  Object.keys(endpoint.pathParams || {}).forEach(k => pathParams[k] = document.querySelector(`[data-path-endpoint="${endpoint.id}"][data-key="${k}"]`)?.value ?? "");
  const queryNode = $(`query-${endpoint.id}`); const bodyNode = $(`body-${endpoint.id}`);
  const query = queryNode ? parseJson(queryNode.value, {}) : {};
  const body = bodyNode ? parseJson(bodyNode.value, null) : null;
  const path = substitutePath(endpoint.path, pathParams);
  return { pathParams, query, body, path, url: buildUrl(path, query) };
}

function updatePreview(endpoint) {
  const node = $(`preview-${endpoint.id}`); if (!node) return;
  try { node.textContent = endpointInput(endpoint).url; } catch (error) { node.textContent = `JSON 错误：${error.message}`; }
}

function syncContextFromData(data) {
  const root = data?.data ?? data;
  if (!root || typeof root !== "object") return;
  const assign = (id, value) => { if (value) $(id).value = String(value); };
  assign("ctxConversationId", root.conversation_id || root.id && root.title !== undefined ? (root.conversation_id || root.id) : null);
  assign("ctxTaskId", root.task_id || (root.operation && root.id ? root.id : null));
  assign("ctxUserMessageId", root.user_message_id);
  assign("ctxAssistantMessageId", root.assistant_message_id);
  assign("ctxBranchId", root.branch_id || root.active_branch_id);
  if (root.id && root.role === "USER") assign("ctxUserMessageId", root.id);
  if (root.id && root.role === "ASSISTANT") assign("ctxAssistantMessageId", root.id);
  if (root.id && root.role) assign("ctxMessageId", root.id);
}

function setRunState(endpointId, result, detail="") {
  const card = $(`ep-${endpointId}`), meta = $(`meta-${endpointId}`), nav = document.querySelector(`[data-nav-status="${endpointId}"]`);
  if (!card || !meta) return; card.classList.remove("pass","fail"); nav?.classList.remove("pass","fail");
  const pass = Boolean(result?.ok); card.classList.add(pass?"pass":"fail"); nav?.classList.add(pass?"pass":"fail");
  meta.innerHTML = `<strong>${pass?"通过":"失败"}</strong><span>${result?.status || "-"} · ${formatDuration(result?.durationMs)}${detail?` · ${escapeHtml(detail)}`:""}</span>`;
}

async function runEndpoint(id) {
  const endpoint = endpointById(id); const button = $(`run-${id}`); const output = $(`output-${id}`); const meta = $(`output-meta-${id}`);
  if (!endpoint) return;
  if (endpoint.dataAuth && !dataToken()) { toast("该接口需要 Token", "error"); return; }
  if (endpoint.controllerAuth && !controllerToken()) { toast("该接口需要 Controller-Token", "error"); return; }
  if (endpoint.auth !== false && !userToken()) { toast("请输入 URL 参数 X-User-Token", "error"); return; }
  if (endpoint.danger && !confirm(`确认执行：${endpoint.title}？`)) return;
  button.disabled = true; button.textContent = endpoint.sse ? "连接中…" : "请求中…"; output.textContent = endpoint.sse ? "正在建立 SSE…" : "正在请求…";
  try {
    const input = endpointInput(endpoint);
    if (endpoint.sse) {
      await runSse(endpoint, input, output, meta); return;
    }
    const result = await sendHttp(endpoint, input); state.results.set(id, result); output.textContent = pretty(result); meta.textContent = `${result.status || "-"} ${result.statusText || ""} · ${formatDuration(result.durationMs)} · ${nowTime()}`;
    setRunState(id, result, result.networkError ? result.error : ""); if (result.ok) syncContextFromData(result.data);
    toast(`${endpoint.title}：${result.ok?"通过":"失败"}`, result.ok?"success":"error");
  } catch (error) {
    output.textContent = pretty({error:String(error)}); meta.textContent = "输入错误"; setRunState(id,{ok:false,status:0,durationMs:0},String(error)); toast(error.message,"error");
  } finally { if (!endpoint.sse || !state.sseAbort.has(id)) { button.disabled=false; button.textContent="发送请求"; } }
}

function parseSseBlock(block) {
  if (!block.trim()) return null; let event="message", id=""; const lines=[];
  for (const raw of block.split(/\r?\n/)) {
    if (raw.startsWith("event:")) event=raw.slice(6).trim();
    else if (raw.startsWith("id:")) id=raw.slice(3).trim();
    else if (raw.startsWith("data:")) lines.push(raw.slice(5).trimStart());
  }
  if (!lines.length) return null; const rawData=lines.join("\n"); let data;
  try { data=JSON.parse(rawData); } catch (_) { data={raw:rawData}; }
  return {id,event,data};
}

async function runSse(endpoint, input, output, meta) {
  state.sseAbort.get(endpoint.id)?.abort();
  const controller = new AbortController();
  state.sseAbort.set(endpoint.id, controller);
  const events=[];
  let terminal=false, lastId="", received=0, reconnects=0;
  const started=performance.now();
  const render=()=>{output.textContent=events.map(e=>`[${e.time}] ${e.id?`#${e.id} `:""}${e.event}\n${pretty(e.data)}`).join("\n\n");output.scrollTop=output.scrollHeight;};

  try {
    while(!terminal && !controller.signal.aborted) {
      let buffer="";
      const headers=requestHeaders(endpoint,"text/event-stream");
      if(lastId) headers["Last-Event-ID"]=lastId;
      try {
        const response=await fetch(input.url,{headers,signal:controller.signal,cache:"no-store"});
        if(!response.ok||!response.body) throw new Error(`SSE HTTP ${response.status}: ${await response.text()}`);
        meta.textContent=reconnects?`SSE 已恢复（第 ${reconnects} 次）`:"SSE 已连接";
        const reader=response.body.getReader(),decoder=new TextDecoder();
        while(true){
          const {value,done}=await reader.read();
          if(value){
            buffer+=decoder.decode(value,{stream:true});
            const blocks=buffer.split(/\r?\n\r?\n/);buffer=blocks.pop()||"";
            for(const block of blocks){
              const parsed=parseSseBlock(block);if(!parsed)continue;
              received++;lastId=parsed.id||lastId;
              if(parsed.event!=="heartbeat"&&parsed.event!=="sse.connected"){
                events.push({...parsed,time:nowTime()});if(events.length>300)events.shift();render();
              }
              if(parsed.event==="entity.selection.required")syncContextFromData(parsed.data);
              if(["task.completed","task.failed","task.stopped","task.waiting_input","entity.selection.required"].includes(parsed.event)){terminal=true;break;}
            }
          }
          if(terminal)break;
          if(done){buffer+=decoder.decode();if(buffer.trim()){const parsed=parseSseBlock(buffer);if(parsed){lastId=parsed.id||lastId;events.push({...parsed,time:nowTime()});terminal=["task.completed","task.failed","task.stopped","task.waiting_input","entity.selection.required"].includes(parsed.event)||terminal;render();}}break;}
        }
      } catch(error) {
        if(error.name==="AbortError")break;
        events.push({time:nowTime(),event:"client.reconnect",id:"",data:{message:String(error),last_event_id:lastId,reconnect_count:reconnects+1}});render();
      }
      if(terminal||controller.signal.aborted)break;
      reconnects+=1;
      meta.textContent=`连接波动，自动从 ${lastId||"起点"} 续传（第 ${reconnects} 次）`;
      await new Promise(resolve=>setTimeout(resolve,Math.min(500*reconnects,5000)));
    }
    const result={ok:terminal,status:terminal?200:0,durationMs:performance.now()-started};
    setRunState(endpoint.id,result,terminal?"已收到终态":`手动断开，Last-Event-ID=${lastId||"-"}`);
    meta.textContent=terminal?`正常结束 · ${received} 个事件`:`订阅已停止 · ${received} 个事件`;
  } finally {
    state.sseAbort.delete(endpoint.id);
    const button=$(`run-${endpoint.id}`);button.disabled=false;button.textContent="重新订阅";
  }
}

function requestCurl(endpoint, input) {
  const parts=[`curl -X ${endpoint.method}`,`'${input.url}'`,`-H 'Accept: ${endpoint.sse?"text/event-stream":"application/json"}'`];
  if (endpoint.dataAuth) parts.push(`-H 'Token: ${dataToken() || "<TOKEN>"}'`);
  if (endpoint.controllerAuth) parts.push(`-H 'Controller-Token: ${controllerToken() || "<CONTROLLER_TOKEN>"}'`);
  if (endpoint.idempotency) parts.push(`-H 'Idempotency-Key: <UUID>'`);
  if (!["GET","HEAD"].includes(endpoint.method) && input.body !== null) { parts.push("-H 'Content-Type: application/json'"); parts.push(`--data-raw '${JSON.stringify(input.body).replace(/'/g,"'\\''")}'`); }
  return parts.join(" \\\n  ");
}

function endpointCard(endpoint,index,items) {
  const ctx=contextValues(), pathParams=resolveTokens(clone(endpoint.pathParams||{}),ctx), query=resolveTokens(clone(endpoint.query||{}),ctx), body=endpoint.body===undefined?undefined:resolveTokens(clone(endpoint.body),ctx);
  const fields=Object.entries(pathParams).map(([k,v])=>`<label class="field-label">路径参数：${escapeHtml(k)}<input data-path-endpoint="${endpoint.id}" data-key="${escapeHtml(k)}" value="${escapeHtml(v)}" /></label>`).join("");
  const current=items.findIndex(x=>x.id===endpoint.id),prev=items[current-1],next=items[current+1];
  return `<article id="ep-${endpoint.id}" class="endpoint-card" data-search="${escapeHtml(`${endpoint.title} ${endpoint.path} ${endpoint.description}`.toLowerCase())}">
    <header class="endpoint-header"><div><div class="endpoint-title-row"><span class="endpoint-number">${String(index+1).padStart(2,"0")}</span><span class="method-badge method-${endpoint.method.toLowerCase()}">${endpoint.sse?"SSE":endpoint.method}</span><h3 class="endpoint-title">${escapeHtml(endpoint.title)}</h3><code class="endpoint-path">${escapeHtml(endpoint.path)}</code></div><p class="endpoint-description">${escapeHtml(endpoint.description)}</p></div><div id="meta-${endpoint.id}" class="endpoint-run-state"><strong>尚未执行</strong><span>-</span></div></header>
    <div class="endpoint-body"><div class="input-pane"><div class="pane-title"><strong>测试输入</strong><span id="preview-${endpoint.id}" class="request-url-preview"></span></div>${fields?`<div class="field-grid">${fields}</div>`:""}${endpoint.query!==undefined?`<div class="field-group"><label>查询参数 JSON</label><textarea id="query-${endpoint.id}" class="code-input">${escapeHtml(pretty(query))}</textarea></div>`:""}${endpoint.body!==undefined?`<div class="field-group"><label>请求体 JSON</label><textarea id="body-${endpoint.id}" class="code-input body-input">${escapeHtml(pretty(body))}</textarea></div>`:""}${endpoint.query===undefined&&endpoint.body===undefined&&!fields?`<div class="empty-state">该接口无需业务参数。</div>`:""}<div class="button-row"><button id="run-${endpoint.id}" class="${endpoint.danger?"danger":"primary"}" data-run="${endpoint.id}">${endpoint.sse?"订阅事件":"发送请求"}</button>${endpoint.sse?`<button data-stop-sse="${endpoint.id}">断开 SSE</button>`:""}<button data-copy-curl="${endpoint.id}">复制 curl</button><button data-reset="${endpoint.id}">恢复示例</button></div></div>
    <div class="output-pane"><div class="output-toolbar"><div><strong>${endpoint.sse?"事件流":"响应"}</strong><span id="output-meta-${endpoint.id}">尚未执行</span></div><div class="button-row"><button class="small" data-copy-output="${endpoint.id}">复制</button><button class="small" data-clear-output="${endpoint.id}">清空</button></div></div><pre id="output-${endpoint.id}" class="json-output ${endpoint.sse?"sse-output":""}">等待请求</pre></div></div>
    <footer class="endpoint-footer"><span>${prev?`<a href="#ep-${prev.id}">← ${escapeHtml(prev.title)}</a>`:"本组第一个接口"}</span><span>${next?`<a href="#ep-${next.id}">${escapeHtml(next.title)} →</a>`:"本组最后一个接口"}</span></footer></article>`;
}

function renderAll() {
  let index=0; $("endpointWorkspace").innerHTML=GROUPS.map(g=>{const items=ENDPOINTS.filter(e=>e.group===g.id);return `<section id="group-${g.id}" class="endpoint-group"><div class="group-heading"><h2>${g.title}</h2><span>${g.description} · ${items.length} 个接口</span></div>${items.map(e=>endpointCard(e,index++,items)).join("")}</section>`}).join("");
  const overview=`<div class="nav-group"><div class="nav-group-title">总览</div><a class="nav-link" href="#overview"><span class="nav-index">00</span><span>运行总览</span></a><a class="nav-link" href="#agent-management"><span class="nav-index">AG</span><span>智能体开关</span></a></div>`;
  let n=0; $("sidebarNav").innerHTML=overview+GROUPS.map(g=>`<div class="nav-group" data-nav-group="${g.id}"><div class="nav-group-title">${g.title}</div>${ENDPOINTS.filter(e=>e.group===g.id).map(e=>`<a class="nav-link" href="#ep-${e.id}" data-nav-endpoint="${e.id}" data-search="${escapeHtml(`${e.title} ${e.path}`.toLowerCase())}"><span class="nav-index">${String(++n).padStart(2,"0")}</span><span>${escapeHtml(e.title)}</span><span class="nav-status" data-nav-status="${e.id}"></span></a>`).join("")}</div>`).join("")+`<div class="nav-group"><div class="nav-group-title">工具</div><a class="nav-link" href="#custom-request"><span class="nav-index">+</span><span>自定义请求</span></a></div>`;
  ENDPOINTS.forEach(updatePreview);
}

function resetEndpoint(id) {
  const e=endpointById(id),ctx=contextValues(),params=resolveTokens(clone(e.pathParams||{}),ctx);
  Object.entries(params).forEach(([k,v])=>{const node=document.querySelector(`[data-path-endpoint="${id}"][data-key="${k}"]`);if(node)node.value=v??"";});
  if ($(`query-${id}`)) $(`query-${id}`).value=pretty(resolveTokens(clone(e.query||{}),ctx));
  if ($(`body-${id}`)) $(`body-${id}`).value=pretty(resolveTokens(clone(e.body),ctx)); updatePreview(e);
}

function applyContext() { ENDPOINTS.forEach(e=>resetEndpoint(e.id)); toast("公共变量已应用", "success"); }
function summaryRows(id,obj) { const node=$(id); node.classList.remove("empty-state"); const entries=Object.entries(obj||{}); node.innerHTML=entries.length?entries.map(([k,v])=>`<div class="summary-row"><span>${escapeHtml(k)}</span><strong>${escapeHtml(v)}</strong></div>`).join(""):`<div class="empty-state">暂无数据</div>`; }
function statusCard(label,value,sub="",tone="") { return `<div class="status-card ${tone}"><span class="label">${escapeHtml(label)}</span><strong class="value">${escapeHtml(value)}</strong><span class="sub">${escapeHtml(sub)}</span></div>`; }

async function refreshOverview(showToast=true) {
  const button=$("refreshOverviewBtn"); button.disabled=true; button.textContent="刷新中…";
  const endpoint=endpointById("diag-overview"), result=await sendHttp(endpoint,{path:endpoint.path,query:{},body:null});
  if (result.ok) {
    const d=result.data.data; const deps=d.dependencies||{}, tenant=d.tenant_data||{}, features=d.production_features||{};
    $("statusCards").innerHTML=[statusCard("PostgreSQL",deps.postgresql?.status||"-","事实源",deps.postgresql?.status==="ok"?"good":"bad"),statusCard("Redis",deps.redis?.status||"-","队列与事件",deps.redis?.status==="ok"?"good":"bad"),statusCard("会话",tenant.conversation_count??0,"当前用户"),statusCard("消息",tenant.message_count??0,"当前用户"),statusCard("运行任务",(tenant.task_counts?.PREPARING||0)+(tenant.task_counts?.PLANNING||0)+(tenant.task_counts?.EXECUTING||0)+(tenant.task_counts?.STREAMING||0),"实时生成"),statusCard("Outbox待投递",tenant.outbox_counts?.PENDING||0,"当前用户",(tenant.outbox_counts?.FAILED||0)>0?"warn":"good")].join("");
    summaryRows("taskSummary",tenant.task_counts); summaryRows("outboxSummary",tenant.outbox_counts); summaryRows("redisSummary",d.redis_stream_lengths); summaryRows("featureSummary",{"Transactional Outbox":features.transactional_outbox?"开启":"关闭","快速投递":features.outbox_fast_publish?"开启":"关闭","PostgreSQL RLS":features.postgresql_rls?"开启":"关闭","OpenTelemetry":features.opentelemetry?"开启":"关闭","采样率":features.otel_sample_ratio}); $("overviewRaw").textContent=pretty(result); setConnection(true,`已连接 · ${formatDuration(result.durationMs)}`);
  } else { setConnection(false,`连接失败 · ${result.error||result.statusText}`); $("overviewRaw").textContent=pretty(result); }
  if(showToast)toast(result.ok?"运行状态已刷新":"刷新失败",result.ok?"success":"error"); button.disabled=false; button.textContent="刷新状态";
}


function healthTone(status) {
  return ({HEALTHY:"good",DEGRADED:"warn",UNHEALTHY:"bad",DISABLED:"muted"})[status] || "muted";
}

function renderAgentCards(rows) {
  const node = $("agentCards");
  if (!rows.length) { node.innerHTML = '<div class="empty-state">暂无已注册智能体</div>'; return; }
  node.innerHTML = rows.map(item => {
    const mandatory = Boolean(item.mandatory);
    const disabled = mandatory ? "disabled" : "";
    const health = item.health_status || "UNKNOWN";
    return `<article class="agent-card" data-agent-id="${escapeHtml(item.agent_id)}">
      <div class="agent-card-head"><div><span class="agent-type">${escapeHtml(item.adapter_type)}</span><h3>${escapeHtml(item.display_name)}</h3><code>${escapeHtml(item.agent_id)}</code></div><span class="health-chip ${healthTone(health)}">${escapeHtml(health)}</span></div>
      <p>${escapeHtml(item.public_description || item.description || "")}</p>
      <div class="agent-switches">
        <label><input type="checkbox" data-agent-switch="enabled" ${item.enabled?"checked":""} ${disabled}/>启用</label>
        <label><input type="checkbox" data-agent-switch="routing_enabled" ${item.routing_enabled?"checked":""} ${disabled}/>参与路由</label>
        <label><input type="checkbox" data-agent-switch="execution_enabled" ${item.execution_enabled?"checked":""} ${disabled}/>允许执行</label>
      </div>
      <div class="agent-meta"><span>适配器：${item.adapter_registered?"已注册":"未注册"}</span><span>准入：${escapeHtml(item.admission_policy_id || "default.v1")}</span><span>实体：${escapeHtml((item.supported_entity_types || []).join(", "))}</span></div>
      <div class="button-row"><button class="small" data-agent-health="${escapeHtml(item.agent_id)}">健康检查</button><button class="small" data-agent-save="${escapeHtml(item.agent_id)}">保存开关</button></div>
      ${item.maintenance_message?`<div class="agent-note">${escapeHtml(item.maintenance_message)}</div>`:""}
    </article>`;
  }).join("");
}

async function refreshAgents(showToast=true) {
  const button=$("refreshAgentsBtn");
  if (!controllerToken()) { if(showToast)toast("请输入 Controller-Token","error"); return; }
  button.disabled=true; button.textContent="刷新中…";
  const endpoint=endpointById("agents-list");
  const result=await sendHttp(endpoint,{path:endpoint.path,query:{},body:null});
  if(result.ok) renderAgentCards(result.data?.data || []);
  else $("agentCards").innerHTML=`<div class="empty-state">加载失败：${escapeHtml(result.data?.message || result.error || result.statusText)}</div>`;
  if(showToast)toast(result.ok?"智能体状态已刷新":"智能体状态加载失败",result.ok?"success":"error");
  button.disabled=false; button.textContent="刷新智能体";
}

async function saveAgentSwitches(agentId) {
  const card=document.querySelector(`[data-agent-id="${CSS.escape(agentId)}"]`);
  const payload={};
  card.querySelectorAll("[data-agent-switch]").forEach(input=>payload[input.dataset.agentSwitch]=input.checked);
  const endpoint=endpointById("agents-update");
  const path=substitutePath(endpoint.path,{agent_id:agentId});
  const result=await sendHttp(endpoint,{path,query:{},body:payload});
  toast(result.ok?"智能体开关已保存":(result.data?.message||"保存失败"),result.ok?"success":"error");
  if(result.ok) await refreshAgents(false);
}

async function checkAgentHealth(agentId) {
  const endpoint=endpointById("agents-health");
  const path=substitutePath(endpoint.path,{agent_id:agentId});
  const result=await sendHttp(endpoint,{path,query:{},body:null});
  toast(result.ok?"健康检查完成":(result.data?.message||"健康检查失败"),result.ok?"success":"error");
  if(result.ok) await refreshAgents(false);
}

function setConnection(ok,text,running=false){$("connectionDot").className=`status-dot ${running?"running":ok?"ok":"bad"}`;$("connectionText").textContent=text;}
async function connectCheck(){const b=$("connectBtn");b.disabled=true;b.textContent="检查中…";setConnection(false,"正在连接…",true);const e=endpointById("ready"),r=await sendHttp(e,{path:e.path,query:{},body:null});setConnection(r.ok,r.ok?`连接正常 · ${formatDuration(r.durationMs)}`:`连接失败 · ${r.error||r.statusText}`);b.disabled=false;b.textContent="连接检查";if(r.ok)await refreshOverview(false);}

function filterEndpoints(q){q=q.trim().toLowerCase();document.querySelectorAll("[data-nav-endpoint]").forEach(x=>x.style.display=!q||x.dataset.search.includes(q)?"flex":"none");document.querySelectorAll("[data-nav-group]").forEach(g=>g.style.display=[...g.querySelectorAll("[data-nav-endpoint]")].some(x=>x.style.display!=="none")?"block":"none");document.querySelectorAll(".endpoint-card").forEach(c=>c.style.display=!q||c.dataset.search.includes(q)?"block":"none");document.querySelectorAll(".endpoint-group").forEach(g=>g.style.display=[...g.querySelectorAll(".endpoint-card")].some(c=>c.style.display!=="none")?"block":"none");}

async function sendCustom(){const b=$("sendCustomBtn");b.disabled=true;try{const endpoint={method:$("customMethod").value,auth:true,dataAuth:Boolean(dataToken())};const path=$("customPath").value.trim(),query=parseJson($("customQuery").value,{}),body=parseJson($("customBody").value,{});const r=await sendHttp(endpoint,{path,query,body:["GET","HEAD"].includes(endpoint.method)?null:body});$("customOutput").textContent=pretty(r);$("customMeta").textContent=`${r.status||"-"} · ${formatDuration(r.durationMs)}`;}catch(e){$("customOutput").textContent=pretty({error:String(e)});}finally{b.disabled=false;}}

function saveSettings(){localStorage.setItem(STORAGE.api,apiBase());localStorage.setItem(STORAGE.user,userToken());localStorage.setItem(STORAGE.data,dataToken());localStorage.setItem(STORAGE.controller,controllerToken());toast("连接设置已保存","success");}
function setAuto(enabled){if(state.autoTimer)clearInterval(state.autoTimer);state.autoTimer=null;localStorage.setItem(STORAGE.auto,enabled?"1":"0");if(enabled)state.autoTimer=setInterval(()=>refreshOverview(false),5000);}

function bindEvents(){
  $("apiBase").value=localStorage.getItem(STORAGE.api)||window.location.origin;$("userToken").value=localStorage.getItem(STORAGE.user)||"user_10086";$("dataToken").value=localStorage.getItem(STORAGE.data)||"";$("controllerToken").value=localStorage.getItem(STORAGE.controller)||"";
  $("saveSettingsBtn").onclick=saveSettings;$("connectBtn").onclick=connectCheck;$("refreshOverviewBtn").onclick=()=>refreshOverview(true);$("refreshAgentsBtn").onclick=()=>refreshAgents(true);$("applyContextBtn").onclick=applyContext;$("endpointSearch").oninput=e=>filterEndpoints(e.target.value);$("mobileMenuBtn").onclick=()=>document.querySelector(".sidebar").classList.toggle("open");
  $("autoRefreshToggle").checked=localStorage.getItem(STORAGE.auto)==="1";$("autoRefreshToggle").onchange=e=>setAuto(e.target.checked);setAuto($("autoRefreshToggle").checked);
  document.addEventListener("click",async e=>{const agentSave=e.target.closest("[data-agent-save]");if(agentSave){await saveAgentSwitches(agentSave.dataset.agentSave);return;}const agentHealth=e.target.closest("[data-agent-health]");if(agentHealth){await checkAgentHealth(agentHealth.dataset.agentHealth);return;}const run=e.target.closest("[data-run]");if(run)runEndpoint(run.dataset.run);const stop=e.target.closest("[data-stop-sse]");if(stop){state.sseAbort.get(stop.dataset.stopSse)?.abort();state.sseAbort.delete(stop.dataset.stopSse);}const copy=e.target.closest("[data-copy-curl]");if(copy){try{const ep=endpointById(copy.dataset.copyCurl);copyText(requestCurl(ep,endpointInput(ep)),"curl 已复制");}catch(err){toast(err.message,"error");}}const reset=e.target.closest("[data-reset]");if(reset)resetEndpoint(reset.dataset.reset);const out=e.target.closest("[data-copy-output]");if(out)copyText($(`output-${out.dataset.copyOutput}`).textContent,"输出已复制");const clear=e.target.closest("[data-clear-output]");if(clear){$(`output-${clear.dataset.clearOutput}`).textContent="等待请求";$(`output-meta-${clear.dataset.clearOutput}`).textContent="尚未执行";}});
  document.addEventListener("input",e=>{const card=e.target.closest(".endpoint-card");if(card)updatePreview(endpointById(card.id.replace("ep-","")));});
  $("sendCustomBtn").onclick=sendCustom;$("copyCustomBtn").onclick=()=>{const endpoint={method:$("customMethod").value,auth:true,dataAuth:Boolean(dataToken())};const input={url:buildUrl($("customPath").value.trim(),parseJson($("customQuery").value,{})),body:parseJson($("customBody").value,{})};copyText(requestCurl(endpoint,input),"curl 已复制");};$("clearCustomBtn").onclick=()=>{$("customOutput").textContent="等待请求";$('customMeta').textContent="尚未执行";};
}

function init(){renderAll();bindEvents();connectCheck();if(controllerToken())refreshAgents(false);}
document.addEventListener("DOMContentLoaded",init);