"use strict";

const CLIENT_SESSION_ID = (globalThis.crypto?.randomUUID?.() || `session_${Date.now()}_${Math.random().toString(36).slice(2)}`);

const CHAT_UI_BUILD = "1.4.0";
window.__CHAT_UI_BUILD__ = CHAT_UI_BUILD;

const $ = (id) => document.getElementById(id);
const STORAGE = {
  api: "conversationChatApiBase",
  user: "conversationChatUserToken",
  data: "conversationChatDataToken",
  app: "conversationChatAppCode",
  mode: "conversationChatExecutionMode",
};
const VALID_MODES = new Set(["quick", "normal", "expert"]);
const TERMINAL_EVENTS = new Set(["task.completed", "task.failed", "task.stopped", "task.waiting_input"]);
const TERMINAL_STATUSES = new Set(["COMPLETED", "FAILED", "STOPPED", "TIMEOUT", "WAITING_INPUT"]);
const ASR_REALTIME_FINISH_GRACE_MS = 120;

const state = {
  conversations: [],
  currentConversationId: null,
  currentConversation: null,
  messages: [],
  currentTaskId: null,
  currentAssistantId: null,
  streamAbort: null,
  streamLastEventId: "0-0",
  terminalReceived: false,
  lastStreamEventAt: 0,
  lastBusinessEventAt: 0,
  currentStreamUrl: null,
  lastStreamRequestUrl: null,
  lastStreamError: null,
  persistenceSyncCount: 0,
  appliedContentEventIds: new Set(),
  executionRenderPending: false,
  traceEvents: [],
  messageEvents: new Map(),
  streamEpoch: 0,
  diagnosisConfirmation: null,
  streamOutputs: new Map(),
  appliedAnswerEvents: new Set(),
  processedStreamEvents: new Set(),
  settledDiagnosisIds: new Set(),
  persistenceCursors: new Map(),
  isGenerating: false,
  pendingAttachments: [],
  uploadingAttachments: 0,
  entityCandidates: [],
  entitySelectionTaskId: null,
  entitySelectionExpiresAt: null,
  asrRecording: false,
  asrConnecting: false,
  asrFinalizing: false,
  asrStream: null,
  asrSocket: null,
  asrAudioContext: null,
  asrSourceNode: null,
  asrWorkletNode: null,
  asrSilentGain: null,
  asrBaseText: "",
  asrCommittedText: "",
  asrPartialText: "",
  asrFinishTimer: null,
  asrSessionId: 0,
  executionMode: VALID_MODES.has(localStorage.getItem(STORAGE.mode)) ? localStorage.getItem(STORAGE.mode) : "normal",
};

function apiBase() { return ($("apiBase").value.trim() || window.location.origin).replace(/\/+$/, ""); }
function userToken() { return $("userToken").value.trim(); }
function dataToken() { return $("dataToken").value.trim(); }
function appCode() { return $("appCode").value.trim() || "xiaoao"; }
function executionMode() { return VALID_MODES.has(state.executionMode) ? state.executionMode : "normal"; }
function setExecutionMode(mode) {
  state.executionMode = VALID_MODES.has(mode) ? mode : "normal";
  localStorage.setItem(STORAGE.mode, state.executionMode);
  for (const value of VALID_MODES) $( `${value}ModeBtn` )?.classList.toggle("active", value === state.executionMode);
  updateComposerHint();
}
function uuid() { return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c])); }
function pretty(value) { return typeof value === "string" ? value : JSON.stringify(value, null, 2); }
function formatTime(value) { if (!value) return ""; const date = new Date(value); return Number.isNaN(date.getTime()) ? "" : date.toLocaleString(); }
function modeLabel(mode) { return ({quick:"快速", normal:"正常", expert:"专家"})[mode] || mode || "正常"; }

function toast(message, type="info", timeout=3200) {
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  item.textContent = message;
  $("toastContainer").appendChild(item);
  setTimeout(() => item.remove(), timeout);
}

async function copyPlainText(value, successMessage="已复制") {
  const text = String(value ?? "");
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
    else throw new Error("clipboard unavailable");
  } catch (_) {
    const area = document.createElement("textarea");
    area.value = text; area.readOnly = true; area.style.position = "fixed"; area.style.left = "-9999px";
    document.body.appendChild(area); area.select();
    if (!document.execCommand("copy")) throw new Error("浏览器拒绝复制");
    area.remove();
  }
  toast(successMessage, "success");
}

function sanitizeFinalAnswer(source) {
  return String(source || "")
    .replace(/<\s*(think|analysis|reasoning)\s*>[\s\S]*?<\s*\/\s*\1\s*>/gi, "")
    .replace(/<\s*\/?\s*(think|analysis|reasoning)\s*>/gi, "")
    .trimStart();
}

function compactPreview(source, maxLength=72) {
  const text = sanitizeFinalAnswer(source)
    .replace(/```[\s\S]*?```/g, " [代码] ")
    .replace(/[#>*_~`|\[\](){}]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return text.length > maxLength ? `${text.slice(0, maxLength)}…` : text;
}
function normalizeMessageRole(value) { return String(value || "").trim().toUpperCase(); }
function normalizeMessageRows(value) { return Array.isArray(value) ? value.filter(item => item && typeof item === "object") : []; }
function messageMetadata(message) {
  const raw = message?.metadata_json;
  if (raw && typeof raw === "object" && !Array.isArray(raw)) return raw;
  if (typeof raw === "string") { try { const parsed=JSON.parse(raw); return parsed && typeof parsed === "object" ? parsed : {}; } catch (_) {} }
  return {};
}

function renderMarkdown(source) {
  const text = sanitizeFinalAnswer(source).replace(/\r\n?/g, "\n");
  if (!text.trim()) return "<p></p>";
  const codeBlocks = [];
  const inlineCodes = [];
  const protectedText = text.replace(/```([^\n`]*)\n([\s\S]*?)```/g, (_, language, code) => {
    const token = `@@BLOCK_CODE_${codeBlocks.length}@@`;
    codeBlocks.push(`<pre class="markdown-code"><div class="markdown-code-language">${escapeHtml(language.trim() || "text")}</div><code>${escapeHtml(code.replace(/\n$/, ""))}</code></pre>`);
    return `\n${token}\n`;
  }).replace(/`([^`\n]+)`/g, (_, code) => {
    const token = `@@INLINECODE${inlineCodes.length}@@`;
    inlineCodes.push(`<code>${escapeHtml(code)}</code>`);
    return token;
  });
  const inline = (value) => {
    let output = escapeHtml(value);
    output = output.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
    output = output.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    output = output.replace(/__([^_\n]+)__/g, "<strong>$1</strong>");
    output = output.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
    output = output.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
    inlineCodes.forEach((html, index) => { output = output.replaceAll(`@@INLINECODE${index}@@`, html); });
    return output;
  };
  const lines = protectedText.split("\n");
  const output = []; let paragraph = []; let listType = null; let listItems = []; let quoteLines = [];
  const flushParagraph = () => { if (paragraph.length) { output.push(`<p>${paragraph.map(inline).join("<br>")}</p>`); paragraph = []; } };
  const flushList = () => { if (listItems.length) { const tag = listType === "ol" ? "ol" : "ul"; output.push(`<${tag}>${listItems.map(item => `<li>${inline(item)}</li>`).join("")}</${tag}>`); listItems=[]; listType=null; } };
  const flushQuote = () => { if (quoteLines.length) { output.push(`<blockquote>${quoteLines.map(inline).join("<br>")}</blockquote>`); quoteLines=[]; } };
  const flushAll = () => { flushParagraph(); flushList(); flushQuote(); };
  for (let index=0; index<lines.length; index+=1) {
    const line=lines[index], trimmed=line.trim();
    if (/^@@BLOCK_CODE_\d+@@$/.test(trimmed)) { flushAll(); output.push(trimmed); continue; }
    if (!trimmed) { flushAll(); continue; }
    const next=lines[index+1] || "";
    if (line.includes("|") && /^\s*\|?\s*:?-{3,}/.test(next) && next.includes("|")) {
      flushAll(); const header=line.replace(/^\s*\||\|\s*$/g,"").split("|").map(x=>x.trim());
      const aligns=next.replace(/^\s*\||\|\s*$/g,"").split("|").map(x=>/^:-+:$/.test(x.trim())?"center":/^-+:$/.test(x.trim())?"right":"left");
      const rows=[]; index+=2; while(index<lines.length && lines[index].includes("|") && lines[index].trim()){rows.push(lines[index].replace(/^\s*\||\|\s*$/g,"").split("|").map(x=>x.trim()));index+=1;} index-=1;
      output.push(`<div class="markdown-table-wrap"><table><thead><tr>${header.map((x,i)=>`<th style="text-align:${aligns[i]||"left"}">${inline(x)}</th>`).join("")}</tr></thead><tbody>${rows.map(row=>`<tr>${header.map((_,i)=>`<td style="text-align:${aligns[i]||"left"}">${inline(row[i]||"")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`); continue;
    }
    const heading=line.match(/^(#{1,6})\s+(.+)$/); if(heading){flushAll();output.push(`<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`);continue;}
    if(/^\s*(---+|___+|\*\*\*+)\s*$/.test(line)){flushAll();output.push("<hr>");continue;}
    const quote=line.match(/^\s*>\s?(.*)$/); if(quote){flushParagraph();flushList();quoteLines.push(quote[1]);continue;}
    const ul=line.match(/^\s*[-*+]\s+(.+)$/); if(ul){flushParagraph();flushQuote();if(listType&&listType!=="ul")flushList();listType="ul";listItems.push(ul[1]);continue;}
    const ol=line.match(/^\s*\d+[.)]\s+(.+)$/); if(ol){flushParagraph();flushQuote();if(listType&&listType!=="ol")flushList();listType="ol";listItems.push(ol[1]);continue;}
    flushList(); flushQuote(); paragraph.push(line);
  }
  flushAll(); let html=output.join(""); codeBlocks.forEach((block,index)=>{html=html.replaceAll(`@@BLOCK_CODE_${index}@@`,block);}); return html || "<p></p>";
}

function saveLocalSettings() {
  localStorage.setItem(STORAGE.api, $("apiBase").value.trim());
  localStorage.setItem(STORAGE.user, $("userToken").value.trim());
  localStorage.setItem(STORAGE.data, $("dataToken").value);
  localStorage.setItem(STORAGE.app, $("appCode").value.trim());
}
function loadLocalSettings() {
  $("apiBase").value = localStorage.getItem(STORAGE.api) || window.location.origin;
  const savedUser = localStorage.getItem(STORAGE.user);
  const demoUser = savedUser || localStorage.getItem(STORAGE.user + ":demo") || ("demo_" + uuid());
  if (!savedUser) localStorage.setItem(STORAGE.user + ":demo", demoUser);
  $("userToken").value = demoUser;
  $("dataToken").value = localStorage.getItem(STORAGE.data) || "";
  $("appCode").value = localStorage.getItem(STORAGE.app) || "xiaoao";
}
function appendUserToken(path) {
  const url = new URL(path.startsWith("http") ? path : `${apiBase()}${path}`, window.location.origin);
  url.searchParams.set("X-User-Token", userToken());
  return url.toString();
}
async function apiRequest(path, {method="GET", query=null, body=null, generation=false, signal=null, idempotency=false}={}) {
  let url = appendUserToken(path);
  if (query) { const parsed=new URL(url); Object.entries(query).forEach(([k,v])=>{if(v!==null&&v!==undefined&&v!=="")parsed.searchParams.set(k,v);}); url=parsed.toString(); }
  const headers={}; if(body!==null)headers["Content-Type"]="application/json"; if(generation)headers.Token=dataToken(); if(idempotency)headers["Idempotency-Key"]=uuid();
  const response=await fetch(url,{method,headers,body:body===null?undefined:JSON.stringify(body),signal});
  if(response.status===204)return {data:null};
  const payload=await response.json().catch(()=>({}));
  if(!response.ok){const error=new Error(payload?.error?.message || payload?.detail || `HTTP ${response.status}`);error.status=response.status;throw error;}
  return payload;
}

function setConnection(status,text){const badge=$("connectionBadge");badge.className=`badge ${status}`;badge.textContent=text;}
async function testConnection(showToast=true){try{const response=await fetch(`${apiBase()}/ready`);if(!response.ok)throw new Error(`HTTP ${response.status}`);setConnection("good","已连接");if(showToast)toast("连接正常","success");return true;}catch(error){setConnection("bad","不可用");if(showToast)toast(`连接失败：${error.message}`,"error");return false;}}
function updateComposerHint(){const configured=userToken()&&dataToken();$("composerHint").textContent=configured?`用户 ${userToken()} · ${appCode()} · ${modeLabel(executionMode())}模式`:"需要配置用户 Token 和数据访问 Token";}
function openModal(id){$(id).classList.remove("hidden");}
function closeModal(id){$(id).classList.add("hidden");}


function browserAsrSupport() {
  if (!window.isSecureContext) return {ok:false, reason:"浏览器仅允许 HTTPS 或 localhost 页面使用麦克风"};
  if (!navigator.mediaDevices?.getUserMedia) return {ok:false, reason:"当前浏览器不支持麦克风采集"};
  if (!window.AudioContext && !window.webkitAudioContext) return {ok:false, reason:"当前浏览器不支持 Web Audio"};
  if (typeof WebSocket === "undefined") return {ok:false, reason:"当前浏览器不支持 WebSocket"};
  return {ok:true, reason:""};
}
function renderAsrUi() {
  const button = $("micBtn");
  const status = $("asrLiveStatus");
  if (!button || !status) return;
  const unsupported = browserAsrSupport();
  if (!unsupported.ok && !state.asrRecording && !state.asrConnecting) {
    button.disabled = true;
    button.title = unsupported.reason;
    button.textContent = window.isSecureContext ? "🎙️ 不支持" : "🎙️ 需 HTTPS";
    button.classList.remove("recording");
    button.setAttribute("aria-pressed", "false");
    status.classList.add("hidden");
    return;
  }
  const active = state.asrRecording || state.asrConnecting;
  const busy = state.isGenerating || state.uploadingAttachments > 0;
  button.disabled = !active && (busy || state.asrFinalizing);
  button.classList.toggle("recording", active);
  button.setAttribute("aria-pressed", active ? "true" : "false");
  button.textContent = active ? "⏹ 停止语音" : "🎙️ 语音";
  if (state.asrConnecting) {
    status.classList.remove("hidden");
    status.querySelector("b").textContent = "正在连接实时识别...";
  } else if (state.asrRecording) {
    status.classList.remove("hidden");
    status.querySelector("b").textContent = "实时听写中...";
  } else if (state.asrFinalizing) {
    status.classList.remove("hidden");
    status.querySelector("b").textContent = "正在确认最后文字...";
  } else {
    status.classList.add("hidden");
  }
}
function joinAsrText(existing, incoming) {
  const left = String(existing || "");
  const right = String(incoming || "").trim();
  if (!right) return left;
  if (!left) return right;
  if (/\s$/.test(left)) return `${left}${right}`;
  if (/[A-Za-z0-9]$/.test(left) && /^[A-Za-z0-9]/.test(right)) return `${left} ${right}`;
  return `${left}${right}`;
}
function renderAsrTextPreview() {
  const input = $("messageInput");
  if (!input) return;
  const recognized = joinAsrText(state.asrCommittedText, state.asrPartialText);
  input.value = joinAsrText(state.asrBaseText, recognized);
  autoResizeInput();
  input.dispatchEvent(new Event("input", {bubbles:true}));
  input.focus();
  input.selectionStart = input.selectionEnd = input.value.length;
}
function realtimeAsrWebSocketUrl(language="zh") {
  const base = new URL(apiBase(), window.location.href);
  base.protocol = base.protocol === "https:" ? "wss:" : "ws:";
  base.pathname = "/chat/v1/asr/realtime";
  base.search = "";
  base.searchParams.set("X-User-Token", userToken());
  if (language) base.searchParams.set("language", language);
  return base.toString();
}
async function releaseAsrCapture({closeSocket=false}={}) {
  if (state.asrFinishTimer) clearTimeout(state.asrFinishTimer);
  state.asrFinishTimer = null;
  try { state.asrWorkletNode?.disconnect(); } catch (_) {}
  try { state.asrSourceNode?.disconnect(); } catch (_) {}
  try { state.asrSilentGain?.disconnect(); } catch (_) {}
  if (state.asrStream) for (const track of state.asrStream.getTracks()) track.stop();
  if (state.asrAudioContext && state.asrAudioContext.state !== "closed") {
    try { await state.asrAudioContext.close(); } catch (_) {}
  }
  state.asrStream = null;
  state.asrAudioContext = null;
  state.asrSourceNode = null;
  state.asrWorkletNode = null;
  state.asrSilentGain = null;
  if (closeSocket && state.asrSocket && state.asrSocket.readyState < WebSocket.CLOSING) {
    try { state.asrSocket.close(1000, "client cleanup"); } catch (_) {}
  }
  if (closeSocket) state.asrSocket = null;
}
async function startPcmCapture(sessionId) {
  if (sessionId !== state.asrSessionId || !state.asrStream || !state.asrAudioContext) return;
  const context = state.asrAudioContext;
  const workletUrl = new URL("./pcm-capture-worklet-v1.0.0.6.js", window.location.href).href;
  await context.audioWorklet.addModule(workletUrl);
  const source = context.createMediaStreamSource(state.asrStream);
  const worklet = new AudioWorkletNode(context, "pcm16-capture", {numberOfInputs:1, numberOfOutputs:1, channelCount:1});
  const silentGain = context.createGain();
  silentGain.gain.value = 0;
  worklet.port.onmessage = event => {
    if (sessionId !== state.asrSessionId) return;
    const buffer = event.data?.buffer;
    const socket = state.asrSocket;
    if (event.data?.type === "pcm" && buffer && socket?.readyState === WebSocket.OPEN && (state.asrRecording || state.asrFinalizing)) {
      socket.send(buffer);
    }
  };
  source.connect(worklet);
  worklet.connect(silentGain);
  silentGain.connect(context.destination);
  state.asrSourceNode = source;
  state.asrWorkletNode = worklet;
  state.asrSilentGain = silentGain;
  if (context.state === "suspended") await context.resume();
}
function handleRealtimeAsrMessage(event, sessionId) {
  if (sessionId !== state.asrSessionId) return;
  let payload;
  try { payload = JSON.parse(event.data); } catch (_) { return; }
  const type = String(payload?.type || "");
  if (type === "upstream.connected") {
    state.asrConnecting = true;
    renderAsrUi();
    return;
  }
  if (type === "ready") {
    state.asrConnecting = false;
    state.asrRecording = true;
    renderAsrUi();
    updateGeneratingUi();
    startPcmCapture(sessionId).catch(error=>{
      toast(`无法启动实时音频处理：${error.message}`, "error", 6000);
      abortAsrSession();
    });
    return;
  }
  if (type === "partial") {
    state.asrPartialText = String(payload.text || "");
    renderAsrTextPreview();
    return;
  }
  if (type === "final") {
    const finalText = String(payload.text || "").trim();
    if (finalText) state.asrCommittedText = joinAsrText(state.asrCommittedText, finalText);
    state.asrPartialText = "";
    renderAsrTextPreview();
    return;
  }
  if (type === "error") {
    toast(`实时语音识别失败：${payload.message || payload.code || "未知错误"}`, "error", 6000);
    abortAsrSession();
    return;
  }
  if (type === "finished") {
    finishAsrSessionCleanup();
  }
}
async function finishAsrSessionCleanup() {
  const socket = state.asrSocket;
  state.asrRecording = false;
  state.asrConnecting = false;
  state.asrFinalizing = false;
  await releaseAsrCapture({closeSocket:false});
  if (socket && socket.readyState < WebSocket.CLOSING) {
    try { socket.close(1000, "finished"); } catch (_) {}
  }
  state.asrSocket = null;
  renderAsrUi();
  updateGeneratingUi();
  $("messageInput")?.focus();
}
async function abortAsrSession() {
  state.asrRecording = false;
  state.asrConnecting = false;
  state.asrFinalizing = false;
  await releaseAsrCapture({closeSocket:true});
  state.asrSocket = null;
  renderAsrUi();
  updateGeneratingUi();
}
async function startAsrRecording() {
  if (state.asrRecording || state.asrConnecting || state.asrFinalizing) return;
  const support = browserAsrSupport();
  if (!support.ok) { toast(support.reason, "error", 6000); renderAsrUi(); return; }
  if (!userToken()) { openModal("settingsModal"); toast("请先配置 X-User-Token", "info"); return; }

  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  let context = null;
  try {
    context = new AudioContextClass({latencyHint:"interactive"});
    const status = await apiRequest("/chat/v1/asr/status");
    if (!status?.data?.realtime_enabled) throw new Error("后端实时 ASR 功能未启用");
    const stream = await navigator.mediaDevices.getUserMedia({
      audio:{channelCount:1, echoCancellation:true, noiseSuppression:true, autoGainControl:true},
      video:false,
    });
    state.asrSessionId += 1;
    const sessionId = state.asrSessionId;
    state.asrBaseText = $("messageInput")?.value || "";
    state.asrCommittedText = "";
    state.asrPartialText = "";
    state.asrStream = stream;
    state.asrAudioContext = context;
    state.asrConnecting = true;
    state.asrRecording = false;
    state.asrFinalizing = false;
    renderAsrUi();
    updateGeneratingUi();

    const socket = new WebSocket(realtimeAsrWebSocketUrl(status?.data?.default_language || "zh"));
    socket.binaryType = "arraybuffer";
    state.asrSocket = socket;
    const connectTimer = setTimeout(()=>{
      if (sessionId === state.asrSessionId && state.asrConnecting) {
        toast("实时语音识别连接超时", "error");
        abortAsrSession();
      }
    }, 15000);
    socket.onmessage = event => handleRealtimeAsrMessage(event, sessionId);
    socket.onerror = () => {
      if (sessionId !== state.asrSessionId) return;
      clearTimeout(connectTimer);
      toast("实时语音 WebSocket 连接失败，请检查 HTTPS/WSS 与服务端配置", "error", 6000);
    };
    socket.onclose = () => {
      clearTimeout(connectTimer);
      if (sessionId !== state.asrSessionId) return;
      if (state.asrRecording || state.asrConnecting) {
        toast("实时语音连接已断开", "error", 5000);
        abortAsrSession();
      } else if (state.asrFinalizing) {
        finishAsrSessionCleanup();
      }
    };
  } catch (error) {
    if (context && context.state !== "closed") { try { await context.close(); } catch (_) {} }
    const denied = error?.name === "NotAllowedError" || error?.name === "SecurityError";
    toast(denied ? "麦克风权限被拒绝，请在浏览器地址栏允许麦克风访问" : `无法启动实时语音：${error.message}`, "error", 6000);
    await abortAsrSession();
  }
}
function stopAsrRecording() {
  if (!state.asrRecording && !state.asrConnecting) return;
  const wasConnecting = state.asrConnecting && !state.asrRecording;
  state.asrRecording = false;
  state.asrConnecting = false;
  state.asrFinalizing = !wasConnecting;
  renderAsrUi();
  updateGeneratingUi();
  if (wasConnecting) {
    abortAsrSession();
    return;
  }
  try { state.asrWorkletNode?.port.postMessage({type:"flush"}); } catch (_) {}
  state.asrFinishTimer = setTimeout(()=>{
    try { state.asrSourceNode?.disconnect(); } catch (_) {}
    if (state.asrStream) for (const track of state.asrStream.getTracks()) track.stop();
    const socket = state.asrSocket;
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({type:"finish"}));
    else finishAsrSessionCleanup();
  }, ASR_REALTIME_FINISH_GRACE_MS);
}
function toggleAsrRecording() {
  if (state.asrRecording || state.asrConnecting) stopAsrRecording();
  else startAsrRecording();
}

async function uploadAttachment(file){
  const form=new FormData(); form.append("file",file);
  const response=await fetch(appendUserToken("/chat/v1/files"),{method:"POST",body:form});
  const payload=await response.json().catch(()=>({}));
  if(!response.ok){const error=new Error(payload?.error?.message || payload?.detail || `HTTP ${response.status}`);error.status=response.status;throw error;}
  return payload.data;
}
function formatBytes(value){const bytes=Number(value||0);if(bytes<1024)return `${bytes} B`;if(bytes<1024*1024)return `${(bytes/1024).toFixed(1)} KB`;return `${(bytes/1024/1024).toFixed(1)} MB`;}
function attachmentIsImage(item){return String(item?.kind||"").toLowerCase()==="image"||String(item?.mime_type||"").toLowerCase().startsWith("image/");}
function attachmentContentUrl(item){return appendUserToken(item?.content_url || `/chat/v1/files/${item?.attachment_id || item?.id}/content`);}
function renderPendingAttachments(){const box=$("pendingAttachments");if(!state.pendingAttachments.length){box.classList.add("hidden");box.innerHTML="";return;}box.classList.remove("hidden");box.innerHTML=state.pendingAttachments.map(item=>{const url=attachmentContentUrl(item);if(attachmentIsImage(item)){return `<div class="pending-attachment pending-image"><a href="${escapeHtml(url)}" target="_blank" rel="noreferrer" title="预览 ${escapeHtml(item.filename||"图片")}"><img src="${escapeHtml(url)}" alt="${escapeHtml(item.filename||"图片")}" loading="lazy" /></a><div><strong>${escapeHtml(item.filename||"图片")}</strong><small>${escapeHtml(formatBytes(item.size_bytes))}</small></div><button data-remove-attachment="${escapeHtml(item.id)}" aria-label="移除附件">×</button></div>`;}return `<div class="pending-attachment"><span class="attachment-icon">📄</span><div><strong>${escapeHtml(item.filename)}</strong><small>${escapeHtml(formatBytes(item.size_bytes))}</small></div><button data-remove-attachment="${escapeHtml(item.id)}" aria-label="移除附件">×</button></div>`;}).join("");}
async function handleAttachmentFiles(fileList){const files=[...fileList];if(!files.length)return;state.uploadingAttachments+=files.length;updateGeneratingUi();for(const file of files){try{const result=await uploadAttachment(file);state.pendingAttachments.push(result);renderPendingAttachments();}catch(error){toast(`${file.name} 上传失败：${error.message}`,"error");}finally{state.uploadingAttachments-=1;updateGeneratingUi();}}$("attachmentInput").value="";}
async function removePendingAttachment(id){const item=state.pendingAttachments.find(x=>String(x.id)===String(id));state.pendingAttachments=state.pendingAttachments.filter(x=>String(x.id)!==String(id));renderPendingAttachments();if(item)apiRequest(`/chat/v1/files/${id}`,{method:"DELETE"}).catch(()=>{});}
function messageAttachments(message){const rows=messageMetadata(message).attachments;return Array.isArray(rows)?rows:[];}
function renderMessageAttachments(message){const rows=messageAttachments(message);if(!rows.length)return "";return `<div class="message-attachments">${rows.map(item=>{const url=attachmentContentUrl(item);if(attachmentIsImage(item)){return `<a class="message-attachment image" href="${escapeHtml(url)}" target="_blank" rel="noreferrer"><img src="${escapeHtml(url)}" alt="${escapeHtml(item.filename||"图片")}" loading="lazy" /><span>${escapeHtml(item.filename||"图片")}</span></a>`;}return `<a class="message-attachment file" href="${escapeHtml(url)}" target="_blank" rel="noreferrer"><span class="attachment-icon">📎</span><div><strong>${escapeHtml(item.filename||"附件")}</strong><small>${escapeHtml(formatBytes(item.size_bytes))}</small></div></a>`;}).join("")}</div>`;}

function statusText(status){return ({STREAMING:"生成中",COMPLETED:"完成",FAILED:"失败",STOPPED:"已停止",WAITING_INPUT:"等待补充",WAITING_SELECTION:"等待选择",WAITING_CONFIRMATION:"等待分析方式选择",CANCELLED:"已结束",SKIPPED:"已跳过",TIMEOUT:"超时",PENDING:"等待中"})[status]||status||"";}
function messageById(id){return state.messages.find(item=>String(item.id)===String(id));}
function messageExecutionEvents(id){return state.messageEvents.get(String(id)) || [];}
function eventPayload(row){const payload=row?.payload||{};const input=row?.input_payload||payload.input_payload||{};const output=row?.output_payload||payload.output_payload||{};const detail={...payload};if(Object.keys(input).length)detail.input=input;if(Object.keys(output).length)detail.output=output;delete detail.input_payload;delete detail.output_payload;return detail;}
function eventTitle(event){const labels={
  "task.queued":"任务入队","task.started":"任务开始","graph.selected":"选择执行模式","graph.started":"开始执行流程","context.loaded":"上下文加载完成",
  "attachment.preparation.started":"附件处理开始","attachment.preparation.completed":"附件处理完成","attachment.parser.started":"文件解析开始","attachment.parser.completed":"文件解析完成","attachment.search.completed":"附件检索完成",
  "workflow.selected":"业务流程已选择","workflow.not_selected":"进入动态规划","workflow.classification.failed":"问题理解失败",
  "entity.resolution.completed":"设备/测点实体解析","supervisor.plan.completed":"主智能体完成规划","supervisor.evaluation.completed":"主智能体完成证据核验","supervisor.synthesis.completed":"主智能体完成综合",
  "agent.started":"子智能体开始","agent.retrying":"子智能体重试","agent.completed":"子智能体完成","agent.failed":"子智能体失败",
  "tool.started":"MCP/工具调用开始","tool.retrying":"MCP/工具正在重试","tool.completed":"MCP/工具调用完成","tool.failed":"MCP/工具调用失败",
  "performance.span.started":"执行步骤开始","performance.span.completed":"执行步骤完成","performance.span.failed":"执行步骤失败","performance.mcp.details":"历史MCP内部性能明细",
  "entity.selection.pending":"等待实体选择","entity.selection.required":"需要选择实体","clarification.required":"需要补充信息","entity.selection.completed":"实体选择完成","answer.started":"开始生成回答","answer.completed":"回答生成完成","task.completed":"任务完成","task.failed":"任务失败","task.stopped":"任务停止","task.waiting_input":"等待用户补充"
};return labels[event]||event;}
function formatDurationMs(value){const ms=Number(value);if(!Number.isFinite(ms))return "—";if(ms<1)return `${ms.toFixed(3)} ms`;if(ms<1000)return `${ms.toFixed(ms<10?2:1)} ms`;return `${(ms/1000).toFixed(ms<10000?3:2)} s`;}
function perfCategoryLabel(value){return ({llm:"大模型",mcp:"MCP",mcp_tool:"MCP工具",database:"数据库",embedding:"Embedding",reranker:"Reranker",algorithm:"算法",decode:"解码",planning:"规划",workflow:"业务流程",content:"内容处理",context:"上下文",internal:"内部处理"})[value]||value||"流程";}
function taskStartedPayload(events){const row=events.find(item=>item.event_type==="task.started");return row?eventPayload(row):{};}
function executionEventKey(row){const p=eventPayload(row);const seq=row.sequence_no??p.sequence_no??String(row.id||"").split("-",1)[0];return seq?`${seq}:${row.event_type}`:`${row.event_type}:${row.span_id||p.span_id||row.actor_id||""}:${row.created_at||""}`;}
function mcpTraceFromEvent(row){const p=eventPayload(row);return p.execution_trace||p.trace||null;}
function renderMcpInternalTrace(trace){
  const spans=Array.isArray(trace?.spans)?trace.spans:[];
  if(!spans.length)return `<div class="perf-muted">该 MCP 没有返回内部计时明细；这里仅统计 Conversation 测得的整体调用时间。</div>`;
  return `<div class="perf-mcp-tree">${spans.map((item,i)=>`<div class="perf-mcp-row" style="--depth:${item.parent_span_id?1:0}"><span class="perf-status ${item.status==='FAILED'?'failed':'ok'}">${item.status==='FAILED'?'×':'✓'}</span><div><strong>${escapeHtml(item.name||item.code||`内部步骤 ${i+1}`)}</strong><small>${escapeHtml(item.description||"")}</small></div><b>${escapeHtml(formatDurationMs(item.duration_ms))}</b><em>${escapeHtml(perfCategoryLabel(item.category))}</em></div>`).join("")}</div>`;
}
function unifiedExecutionRows(events){
  const rows=(events||[]).filter(row=>row.event_type!=="answer.delta"&&row.event_type!=="performance.mcp.details");
  const hasDetailed=rows.some(row=>String(row.event_type||"").startsWith("performance.span."));
  if(!hasDetailed)return rows;
  const completionBySpan=new Map();
  rows.forEach(row=>{if(["performance.span.completed","performance.span.failed"].includes(row.event_type)){const p=eventPayload(row);const key=String(row.span_id||p.span_id||"");if(key)completionBySpan.set(key,row);}});
  const important=new Set(["task.started","workflow.selected","workflow.not_selected","workflow.classification.failed","entity.resolution.completed","tool.completed","tool.failed","entity.selection.required","entity.selection.completed","answer.started","answer.completed","task.completed","task.failed","task.stopped","task.waiting_input"]);
  const out=[];const seenSpans=new Set();
  rows.forEach(row=>{
    if(row.event_type==="performance.span.started"){
      const p=eventPayload(row);const key=String(row.span_id||p.span_id||"");if(key&&seenSpans.has(key))return;seenSpans.add(key);out.push(completionBySpan.get(key)||row);return;
    }
    if(["performance.span.completed","performance.span.failed"].includes(row.event_type)){
      const p=eventPayload(row);const key=String(row.span_id||p.span_id||"");if(key&&seenSpans.has(key))return;seenSpans.add(key);out.push(row);return;
    }
    if(important.has(row.event_type))out.push(row);
  });
  return out.sort((a,b)=>Number(eventPayload(a).sequence_no??a.sequence_no??0)-Number(eventPayload(b).sequence_no??b.sequence_no??0));
}
function basicExecutionSummary(events){
  const cfg=taskStartedPayload(events);const completed=events.filter(r=>r.event_type==="performance.span.completed");const categories={};
  completed.forEach(row=>{const p=eventPayload(row);const c=p.category||"other";categories[c]=(categories[c]||0)+Number(row.duration_ms||0);});
  const toolCount=events.filter(r=>r.event_type==="tool.completed"||r.event_type==="tool.failed").length;
  return {queue:Number(cfg.queue_wait_ms||0),llm:Number(categories.llm||0),mcp:Number(categories.mcp||0),database:Number(categories.database||0),toolCount,detailed:completed.length>0};
}
function runningExecutionRow(row,events){if(row.event_type!=="performance.span.started")return false;const p=eventPayload(row);const span=String(row.span_id||p.span_id||"");return !events.some(x=>String(x.span_id||eventPayload(x).span_id||"")===span&&["performance.span.completed","performance.span.failed"].includes(x.event_type));}
function renderExecutionEvent(row,index,allEvents){
  const payload=eventPayload(row);const actor=row.actor_id||payload.agent_id||payload.tool_id||payload.tool_name||row.actor_type||"system";const status=row.status||payload.status||"";
  const category=payload.category?perfCategoryLabel(payload.category):"";const desc=payload.description_cn||payload.reason||"";const duration=row.duration_ms!=null?formatDurationMs(row.duration_ms):"";const trace=mcpTraceFromEvent(row);
  const running=runningExecutionRow(row,allEvents);const started=Date.parse(row.created_at||new Date().toISOString());
  const detail={...payload};for(const key of ["content","channel","trace","execution_trace","name_cn","description_cn","category","metrics","internal_monitor_enabled","sequence_no","span_id","parent_span_id","task_id","message_id","graph_mode","created_at","actor_type","actor_id","duration_ms"])delete detail[key];
  const title=payload.name_cn||eventTitle(row.event_type);
  const classes=["execution-item"];if(String(row.event_type||"").startsWith("supervisor."))classes.push("supervisor-step");if(String(row.event_type||"").startsWith("tool."))classes.push("tool-step");if(String(row.event_type||"").startsWith("entity."))classes.push("entity-step");if(running)classes.push("running-step");
  const timeText=running?`<span class="live-duration" data-execution-running-start="${Number.isFinite(started)?started:Date.now()}">运行中</span>`:(duration?escapeHtml(duration):escapeHtml(formatTime(row.created_at||payload.time)));
  const metrics=payload.metrics&&Object.keys(payload.metrics).length?`<details class="execution-data"><summary><span>计时指标</span><code>${escapeHtml(JSON.stringify(payload.metrics).slice(0,160))}</code></summary><pre>${escapeHtml(pretty(payload.metrics))}</pre></details>`:"";
  const traceHtml=trace?`<details class="perf-mcp-detail" open><summary><strong>MCP 内部详细流程</strong><span>${Number(trace.span_count||trace.spans?.length||0)} 步 · ${escapeHtml(formatDurationMs(trace.total_duration_ms))}</span></summary>${renderMcpInternalTrace(trace)}</details>`:(row.event_type==="tool.completed"&&payload.internal_monitor_enabled===false?`<div class="perf-muted compact-note">该 MCP 的内部时间监测未开启，本流程只显示整体调用耗时。</div>`:"");
  return `<div class="${classes.join(" ")}"><div class="execution-index">${running?'●':index+1}</div><div class="execution-step-body"><div class="execution-step-head"><div><strong>${escapeHtml(title)}</strong>${category?`<em class="execution-category">${escapeHtml(category)}</em>`:""}</div><span>${timeText}</span></div>${desc?`<div class="execution-decision">${escapeHtml(desc)}</div>`:""}<div class="execution-field"><span>执行者</span><strong>${escapeHtml(actor)}</strong></div>${status?`<div class="execution-field"><span>状态</span><strong>${escapeHtml(status)}</strong></div>`:""}${metrics}${traceHtml}${Object.keys(detail).length?`<details class="execution-data"><summary><span>执行详情</span><code>${escapeHtml(JSON.stringify(detail).slice(0,160))}</code></summary><pre>${escapeHtml(pretty(detail))}</pre></details>`:""}${row.error_message?`<div class="execution-decision">${escapeHtml(row.error_message)}</div>`:""}</div></div>`;
}
function isFinalContentDelta(eventType,payload={}){return eventType==="answer.delta"||(eventType==="agent.output.delta"&&String(payload?.output_scope||"").toLowerCase()==="final");}
function isFinalContentEventRow(row){return isFinalContentDelta(String(row?.event_type||""),eventPayload(row));}
function renderProcessPanel(message){
  const events=messageExecutionEvents(message.id).filter(row=>!isFinalContentEventRow(row));if(!events.length)return "";
  const rows=unifiedExecutionRows(events);const summary=basicExecutionSummary(events);const running=rows.some(row=>runningExecutionRow(row,events));
  const cards=summary.detailed?`<div class="perf-cards"><div><span>排队等待</span><strong>${formatDurationMs(summary.queue)}</strong></div><div><span>大模型累计</span><strong>${formatDurationMs(summary.llm)}</strong></div><div><span>MCP流程累计</span><strong>${formatDurationMs(summary.mcp)}</strong></div><div><span>数据库明细累计</span><strong>${formatDurationMs(summary.database)}</strong></div></div>`:"";
  const hint=!summary.detailed&&summary.toolCount?`<div class="perf-off-note">详细时间监测未开启；执行流程接口保持不变，仍显示 ${summary.toolCount} 次 MCP/工具整体调用时间。</div>`:"";
  return `<details class="execution-panel unified-execution" open><summary><strong>执行流程${running?' · 实时运行中':''}</strong><span>${rows.length} 个步骤${summary.detailed?' · 已合并详细计时':''}</span></summary>${cards}${hint}<div class="execution-list">${rows.map((row,index)=>renderExecutionEvent(row,index,events)).join("")}</div></details>`;
}
function refreshLiveExecutionTimers(){document.querySelectorAll("[data-execution-running-start]").forEach(target=>{const started=Number(target.dataset.executionRunningStart||Date.now());target.textContent=`已运行 ${Math.max(0,(Date.now()-started)/1000).toFixed(1)} s`;});}
setInterval(refreshLiveExecutionTimers,250);
function scheduleExecutionUiRefresh(){if(state.executionRenderPending)return;state.executionRenderPending=true;requestAnimationFrame(()=>{state.executionRenderPending=false;renderTrace();renderMessages();});}
function addExecutionEvent(row,messageId=null){
  const target=messageId||state.currentAssistantId;const key=executionEventKey(row);
  if(!state.traceEvents.some(item=>executionEventKey(item)===key)){state.traceEvents.push(row);if(state.traceEvents.length>1200)state.traceEvents.shift();}
  if(target){const current=messageExecutionEvents(target);if(!current.some(item=>executionEventKey(item)===key))state.messageEvents.set(String(target),[...current,row]);}
  state.lastStreamEventAt=Date.now();scheduleExecutionUiRefresh();
}
function outputFor(messageId){return state.streamOutputs.get(String(messageId))||null;}
function renderReasoning(message){const out=outputFor(message.id);if(!out?.reasoning)return "";const title=out.reasoningKind==="summary"?"分析摘要":"分析过程";return `<details class="reasoning-output" ${out.folded?"":"open"} data-reasoning-message="${escapeHtml(message.id)}"><summary>${title}</summary><div class="message-text">${renderMarkdown(out.reasoning)}</div></details>`;}
function renderDiagnosisChoice(message){const c=state.diagnosisConfirmation;if(!c||String(c.message_id)!==String(message.id))return "";const expired=Date.parse(c.expires_at)<=Date.now();return `<section class="diagnosis-choice"><p>${expired?"本次选择已过期，未启动详细诊断。":"请选择分析方式；也可以直接在输入框提出其他问题。"}</p>${(c.options||[]).map(o=>`<button data-diagnosis-mode="${escapeHtml(o.mode)}" ${expired||c.submitting?"disabled":""}>${escapeHtml(o.label)}</button>`).join(" ")}</section>`;}
function emptyOutput(generationId=null,sequence=0){return {generationId,generationSequence:sequence,reasoning:"",reasoningKind:null,folded:false,reasoningParts:new Map(),finalParts:new Map(),snapshotSequence:0};}
function orderedParts(parts){return [...parts.entries()].sort((a,b)=>a[0]-b[0]).map(x=>x[1]).join("");}
function applyOutputEvent(type,data,eventId,assistantId=null){
  const mid=String(data.message_id||assistantId||state.currentAssistantId||"");const m=messageById(mid);if(!m)return false;
  const handled=type.startsWith("answer.reasoning.")||["answer.started","answer.completed","answer.failed","answer.cancelled"].includes(type);if(!handled)return false;
  const seq=Number(data.sequence_no||String(eventId||"0").split("-")[0]);let out=outputFor(mid)||emptyOutput();
  if(data.generation_id&&out.generationId!==data.generation_id){if(seq<out.generationSequence)return true;out=emptyOutput(data.generation_id,seq);m.content="";m.status="STREAMING";}
  if(type==="answer.reasoning.delta"){out.reasoningParts.set(seq,String(data.content||""));out.reasoning=orderedParts(out.reasoningParts);out.reasoningKind=data.reasoning_kind;}
  if(type==="answer.completed"&&seq>=out.snapshotSequence){if(data.replace&&typeof data.content==="string")m.content=data.content;m.status=data.status||"COMPLETED";out.folded=true;out.snapshotSequence=seq;}
  if((type==="answer.failed"||type==="answer.cancelled")&&seq>=out.snapshotSequence){m.status=type==="answer.failed"?"FAILED":"STOPPED";out.folded=true;out.snapshotSequence=seq;}
  state.streamOutputs.set(mid,out);return true;
}
function acceptDiagnosisPause(data){if(state.settledDiagnosisIds.has(data.confirmation_id))return;state.diagnosisConfirmation={...data};const m=messageById(data.message_id);if(m){m.status="WAITING_CONFIRMATION";if(data.question)m.content=data.question;}state.isGenerating=false;state.streamAbort?.abort();updateGeneratingUi();renderMessages();}
async function chooseDiagnosisMode(mode){const c=state.diagnosisConfirmation;if(!c||c.submitting)return;c.submitting=true;renderMessages();try{const r=await apiRequest(`/chat/v1/tasks/${c.task_id}/diagnosis-mode`,{method:"POST",body:{confirmation_id:c.confirmation_id,mode}});state.settledDiagnosisIds.add(c.confirmation_id);state.diagnosisConfirmation=null;const m=messageById(c.message_id);if(m){m.status=mode==="cancel"?"STOPPED":"STREAMING";if(mode!=="cancel")m.content="";}renderMessages();if(mode!=="cancel"){state.terminalReceived=false;await streamTask(c.task_id,c.message_id,false);}}catch(error){c.submitting=false;toast(`选择未提交：${error.message}`,"error");renderMessages();}}
async function recoverDiagnosisChoice(){for(const message of [...state.messages].reverse()){const c=message.metadata_json?.diagnosis_confirmation;if(c?.status!=="PENDING"||!c.task_id)continue;try{const r=await apiRequest(`/chat/v1/tasks/${c.task_id}/diagnosis-mode`);if(r.data){state.currentTaskId=c.task_id;state.currentAssistantId=message.id;state.streamLastEventId=r.data.last_event_id||"0-0";acceptDiagnosisPause(r.data);return;}}catch(_){}break;}}

function renderMessages(){
  const list=$("messageList");
  const empty=$("emptyState");
  const messages=normalizeMessageRows(state.messages);
  state.messages=messages;
  const hasMessages=messages.length>0;
  empty.classList.toggle("hidden",hasMessages);
  list.classList.toggle("active",hasMessages);
  list.classList.toggle("has-messages",hasMessages);
  list.setAttribute("aria-hidden",hasMessages?"false":"true");
  if(!hasMessages){list.innerHTML="";return;}

  list.innerHTML=messages.map((message,index)=>{
    try{
      const role=normalizeMessageRole(message.role);
      const assistant=role==="ASSISTANT";
      const streaming=message.status!=="WAITING_CONFIRMATION"&&(message.status==="STREAMING"||(state.isGenerating&&String(message.id)===String(state.currentAssistantId)));
      const content=assistant?sanitizeFinalAnswer(message.content):String(message.content||"");
      const messageId=escapeHtml(message.id || `message-${index}`);
      const alternatives=Number(message.alternative_count||0)>1
        ? `<button data-alt-prev="${messageId}" title="上一个版本">‹</button><span class="version-label">${Number(message.alternative_index||1)}/${Number(message.alternative_count||1)}</span><button data-alt-next="${messageId}" title="下一个版本">›</button>`
        : "";
      const actions=assistant
        ? `<button data-copy-message="${messageId}">复制回答</button><button data-regenerate="${messageId}">重新生成</button>${alternatives}`
        : `<button data-copy-message="${messageId}">复制</button><button data-edit-message="${messageId}">编辑重提</button>${alternatives}`;
      const waitingSelection=assistant&&message.status==="WAITING_SELECTION"&&!content;
      const body=assistant
        ? waitingSelection
          ? `${renderProcessPanel(message)}`
          : `${renderProcessPanel(message)}${renderReasoning(message)}${renderDiagnosisChoice(message)}<section class="final-answer"><div class="final-answer-heading"><strong>最终回答</strong><span>${escapeHtml(statusText(message.status))}</span></div><div class="final-answer-content message-text">${renderMarkdown(content|| (streaming?"正在生成最终回答……":""))}${streaming?'<span class="typing-cursor"></span>':""}</div></section>`
        : `${renderMessageAttachments(message)}<div class="message-text user-message-text">${renderMarkdown(content)}</div>`;
      return `<article class="message-row ${assistant?"assistant":"user"}" data-message-id="${messageId}"><div class="avatar">${assistant?"AI":"你"}</div><div class="message-content"><div class="message-meta"><strong>${assistant?"小奥助手":"你"}</strong><span class="message-status">${escapeHtml(statusText(message.status))}</span><span class="message-status">${escapeHtml(formatTime(message.created_at))}</span></div>${body}<div class="message-actions">${actions}</div></div></article>`;
    }catch(error){
      console.error("chat_message_render_failed",{index,message,error});
      return `<article class="message-row assistant render-error"><div class="avatar">!</div><div class="message-content"><div class="message-meta"><strong>消息显示失败</strong></div><div class="message-text"><p>该条消息无法渲染，请打开浏览器控制台查看错误。</p><pre>${escapeHtml(String(error?.message||error))}</pre></div></div></article>`;
    }
  }).join("");
  requestAnimationFrame(()=>scrollToBottom(false));
}

function renderTrace(){const box=$("traceTimeline");$("traceTaskId").textContent=state.currentTaskId||"尚无任务";if(!state.traceEvents.length){box.innerHTML='<div class="trace-empty">发送问题后，这里会实时显示与消息卡片一致的执行流程和每一步计时。</div>';return;}const events=state.traceEvents.filter(row=>!isFinalContentEventRow(row));const rows=unifiedExecutionRows(events);box.innerHTML=rows.map((row,index)=>{const p=eventPayload(row);const running=runningExecutionRow(row,events);const duration=row.duration_ms!=null?formatDurationMs(row.duration_ms):(running?"运行中":"");const trace=mcpTraceFromEvent(row);return `<div class="trace-event ${running?'running':''}"><div class="trace-dot"></div><div><strong>${escapeHtml(p.name_cn||eventTitle(row.event_type))}</strong><span>${escapeHtml(duration||p.description_cn||row.actor_id||row.actor_type||"")}</span>${p.description_cn&&duration?`<small>${escapeHtml(p.description_cn)}</small>`:""}${trace?renderMcpInternalTrace(trace):""}</div></div>`;}).join("");box.scrollTop=box.scrollHeight;}
function renderConversations(){
  const query=$("conversationSearch").value.trim().toLowerCase();
  const rows=normalizeMessageRows(state.conversations).filter(item=>!query||`${item.title||""} ${item.last_message_preview||""}`.toLowerCase().includes(query));
  $("conversationList").innerHTML=rows.length
    ? rows.map(item=>{
        const title=String(item.title||"新对话");
        const preview=compactPreview(item.last_message_preview||"暂无消息",64)||"暂无消息";
        const active=String(item.id)===String(state.currentConversationId);
        return `<div class="conversation-item ${item.is_pinned?"pinned":""} ${active?"active":""}" data-conversation="${escapeHtml(item.id)}" title="${escapeHtml(title)}"><div class="conversation-copy"><div class="conversation-title">${escapeHtml(title)}</div><div class="conversation-preview">${escapeHtml(preview)}</div></div><div class="conversation-side"><span class="conversation-time">${item.last_message_at?escapeHtml(new Date(item.last_message_at).toLocaleDateString()):""}</span><button type="button" class="conversation-delete" data-delete-conversation="${escapeHtml(item.id)}" data-delete-title="${escapeHtml(title)}" title="删除会话" aria-label="删除会话">×</button></div></div>`;
      }).join("")
    : '<div class="trace-empty">暂无会话</div>';
}

async function loadConversations(){if(!userToken())return;try{const result=await apiRequest("/chat/v1/conversations",{query:{app_code:appCode(),limit:100}});state.conversations=result.data||[];renderConversations();}catch(error){toast(`加载会话失败：${error.message}`,"error");}}
async function loadExecutionEventsForMessages(messages){const assistants=normalizeMessageRows(messages).filter(item=>normalizeMessageRole(item.role)==="ASSISTANT");await Promise.all(assistants.map(async message=>{try{const all=[];let after=0;while(true){const result=await apiRequest(`/chat/v1/messages/${message.id}/execution-events`,{query:{include_reasoning:true,after_sequence:after,limit:1000}});const rows=result.data||[];all.push(...rows);if(rows.length<1000)break;const next=Number(rows[rows.length-1].sequence_no)||after;if(next<=after)break;after=next;}state.messageEvents.set(String(message.id),all.filter(r=>!r.event_type.startsWith("answer.reasoning.")));let out=emptyOutput();for(const r of all){const d=eventPayload(r);if(d.generation_id&&out.generationId!==d.generation_id)out=emptyOutput(d.generation_id,r.sequence_no);if(r.event_type==="answer.reasoning.delta"){out.reasoningParts.set(Number(r.sequence_no),String(d.content||""));out.reasoningKind=d.reasoning_kind;}}out.reasoning=orderedParts(out.reasoningParts);out.folded=true;state.streamOutputs.set(String(message.id),out);}catch(_){state.messageEvents.set(String(message.id),[]);}}));}
async function selectConversation(id){
  if(state.isGenerating)return;
  try{
    const result=await apiRequest(`/chat/v1/conversations/${id}`);
    const detail=result?.data||{};
    let messages=normalizeMessageRows(detail.messages);
    if(!Array.isArray(detail.messages)){
      const messageResult=await apiRequest(`/chat/v1/conversations/${id}/messages`);
      messages=normalizeMessageRows(messageResult?.data);
    }
    state.diagnosisConfirmation=null;
    state.currentConversationId=id;
    state.currentConversation=detail;
    state.messages=messages;
    state.currentStreamUrl=null;
    state.appliedContentEventIds.clear();
    state.traceEvents=[];
    state.messageEvents.clear();
    renderMessages();
    await loadExecutionEventsForMessages(messages);
    $("conversationTitle").textContent=detail.title||"新对话";
    $("branchBadge").textContent=String(detail.active_branch_id||"ROOT").slice(0,8);
    renderConversations();
    renderMessages();
    updateHeaderActions();
    document.querySelector(".chat-sidebar")?.classList.remove("open");
    await recoverConversationEntitySelection(id);
    await recoverDiagnosisChoice();
  }catch(error){
    console.error("chat_conversation_open_failed",error);
    toast(`打开会话失败：${error.message}`,"error");
  }
}
function newChat(){state.streamEpoch+=1;state.diagnosisConfirmation=null;if(state.asrRecording||state.asrConnecting)stopAsrRecording();if(state.streamAbort)state.streamAbort.abort();state.streamAbort=null;state.isGenerating=false;state.terminalReceived=false;state.currentConversationId=null;state.currentConversation=null;state.messages=[];state.currentTaskId=null;state.currentAssistantId=null;state.currentStreamUrl=null;state.appliedContentEventIds.clear();state.traceEvents=[];state.messageEvents.clear();state.pendingAttachments=[];state.entityCandidates=[];state.entitySelectionTaskId=null;state.entitySelectionExpiresAt=null;closeModal("entityModal");renderPendingAttachments();$("conversationTitle").textContent="新对话";$("branchBadge").textContent="ROOT";$("taskBadge").classList.add("hidden");renderConversations();renderMessages();renderTrace();updateHeaderActions();updateGeneratingUi();}
function updateHeaderActions(){const active=Boolean(state.currentConversationId);for(const id of ["pinBtn","renameBtn","deleteBtn"]){$(id).disabled=!active;}$("pinBtn").textContent=state.currentConversation?.is_pinned?"取消置顶":"置顶";}
function autoResizeInput(){const input=$("messageInput");input.style.height="auto";input.style.height=`${Math.min(input.scrollHeight,180)}px`;}
function updateGeneratingUi(){const asrBusy=state.asrRecording||state.asrConnecting||state.asrFinalizing;const busy=state.isGenerating||state.uploadingAttachments>0||asrBusy;$("sendBtn").disabled=busy;$("attachBtn").disabled=state.isGenerating||asrBusy;$("stopBtn").classList.toggle("hidden",!state.isGenerating);for(const mode of VALID_MODES)$( `${mode}ModeBtn` ).disabled=state.isGenerating;renderAsrUi();}
function appendLocalMessage(message){state.messages.push(message);renderMessages();}

async function sendMessage(prefilled=null){
  const content=String(prefilled??$("messageInput").value).trim();if(!content&&!state.pendingAttachments.length)return;if(!userToken()||!dataToken()){openModal("settingsModal");return;}
  if(state.isGenerating&&!state.diagnosisConfirmation)return;
  const attachments=state.pendingAttachments.map(item=>item.id);const attachmentMeta=[...state.pendingAttachments];
  $("messageInput").value="";autoResizeInput();state.pendingAttachments=[];renderPendingAttachments();
  try{
    const result=await apiRequest("/chat/v1/chat/messages",{method:"POST",generation:true,idempotency:true,body:{client_session_id:CLIENT_SESSION_ID,conversation_id:state.currentConversationId,app_code:appCode(),content,attachments,execution_mode:executionMode()}});
    const accepted=result.data;state.diagnosisConfirmation=null;state.currentTaskId=accepted.task_id;state.currentAssistantId=accepted.assistant_message_id;state.currentConversationId=accepted.conversation_id;state.currentStreamUrl=accepted.agent_stream_url||accepted.stream_url||null;state.streamLastEventId="0-0";state.terminalReceived=false;state.appliedContentEventIds.clear();state.traceEvents=[];
    appendLocalMessage({id:accepted.user_message_id,role:"USER",content,status:"COMPLETED",created_at:new Date().toISOString(),metadata_json:{attachments:attachmentMeta,execution_mode:accepted.execution_mode},alternative_count:1,alternative_index:1});
    appendLocalMessage({id:accepted.assistant_message_id,role:"ASSISTANT",content:"",status:"STREAMING",created_at:new Date().toISOString(),metadata_json:{execution_mode:accepted.execution_mode},alternative_count:1,alternative_index:1});
    state.messageEvents.set(String(accepted.assistant_message_id),[]);$("conversationTitle").textContent=accepted.title;await streamTask(accepted.task_id,accepted.assistant_message_id,true);
  }catch(error){state.pendingAttachments=attachmentMeta;renderPendingAttachments();toast(`发送失败：${error.message}`,"error");}
}
function parseSseBlock(block){let id="",event="message",data="",retry=null,hasPayload=false;for(const line of block.split("\n")){if(line.startsWith(":"))continue;if(line.startsWith("id:")){id=line.slice(3).trim();hasPayload=true;}else if(line.startsWith("event:")){event=line.slice(6).trim();hasPayload=true;}else if(line.startsWith("data:")){data+=(data?"\n":"")+line.slice(5).trimStart();hasPayload=true;}else if(line.startsWith("retry:")){retry=Number(line.slice(6).trim())||null;}}let payload={};try{payload=data?JSON.parse(data):{};}catch(_){payload={raw:data};}if(!hasPayload&&retry!==null)event="sse.retry";return{id,event,data:payload,retry};}
function contentEventIdentity(eventType,data,eventId=""){const seq=data?.sequence_no;return String(eventId|| (seq!==undefined&&seq!==null?`${seq}-0`:"") || `${eventType}:${data?.message_id||state.currentAssistantId||""}:${data?.content||""}`);}
function applyFinalContentDelta(eventType,data,eventId="",assistantId=null){
  if(!isFinalContentDelta(eventType,data))return false;const content=String(data?.content||"");if(!content)return true;
  const mid=String(data.message_id||assistantId||state.currentAssistantId);const assistant=messageById(mid);if(!assistant)return true;
  const seq=Number(data.sequence_no||String(eventId||"0").split("-")[0]);
  if(data.output_kind==="confirmation_prompt"){const current=outputFor(mid);if(!current?.generationId){assistant.content=content;assistant.status="WAITING_CONFIRMATION";renderMessages();}return true;}
  if(data.generation_id){let out=outputFor(mid)||emptyOutput(data.generation_id,seq);if(out.generationId!==data.generation_id){if(seq<out.generationSequence)return true;out=emptyOutput(data.generation_id,seq);}out.finalParts.set(seq,content);if(!out.snapshotSequence){assistant.content=orderedParts(out.finalParts);assistant.status="STREAMING";}state.streamOutputs.set(mid,out);}
  else{const key=`${data.task_id||state.currentTaskId}:${contentEventIdentity(eventType,data,eventId)}`;if(state.appliedContentEventIds.has(key))return true;state.appliedContentEventIds.add(key);assistant.content=String(assistant.content||"")+content;assistant.status="STREAMING";}
  renderMessages();return true;
}
function taskStreamUrl(taskId){let raw=state.currentStreamUrl&&String(state.currentTaskId)===String(taskId)?state.currentStreamUrl:`/chat/v1/tasks/${taskId}/events`;const url=new URL(appendUserToken(raw));url.searchParams.set("final_delta_event","agent.output.delta");url.searchParams.set("include_reasoning","true");return url.toString();}
async function handleStreamEvent(parsed){
  if(parsed.data.task_id&&state.currentTaskId&&String(parsed.data.task_id)!==String(state.currentTaskId))return;
  state.lastStreamEventAt=Date.now();if(parsed.id)advanceStreamCursorFromPersistedRow({sequence_no:Number(String(parsed.id).split("-")[0])});if(["heartbeat","sse.connected","sse.warning","sse.retry"].includes(parsed.event)){if(parsed.event==="sse.warning")showStreamNotice(parsed.data.message||"事件流正在重试",true);return;}
  const eventKey=parsed.id?`${parsed.data.task_id||state.currentTaskId}:${parsed.id}`:null;
  if(eventKey&&state.processedStreamEvents.has(eventKey))return;if(eventKey)state.processedStreamEvents.add(eventKey);
  state.lastBusinessEventAt=Date.now();
  const row={id:parsed.id||`${Date.now()}`,sequence_no:parsed.data.sequence_no,event_type:parsed.event,span_id:parsed.data.span_id||null,parent_span_id:parsed.data.parent_span_id||null,duration_ms:parsed.data.duration_ms??null,payload:parsed.data,actor_type:parsed.data.actor_type||"runtime",actor_id:parsed.data.actor_id||parsed.data.agent_id||parsed.data.tool_name||null,status:parsed.data.status||null,error_message:parsed.data.error_message||null,created_at:parsed.data.created_at||parsed.data.time||new Date().toISOString()};
  const outputEvent=applyOutputEvent(parsed.event,parsed.data,parsed.id,state.currentAssistantId);
  const finalDelta=applyFinalContentDelta(parsed.event,parsed.data,parsed.id,state.currentAssistantId);
  if(!finalDelta&&!parsed.event.startsWith("answer.reasoning."))addExecutionEvent(row,state.currentAssistantId);
  if(outputEvent)scheduleExecutionUiRefresh();
  if(parsed.event==="diagnosis.mode.required"&&!parsed.fromHistory&&!state.settledDiagnosisIds.has(parsed.data.confirmation_id)){const id=state.currentTaskId;const pending=await apiRequest(`/chat/v1/tasks/${id}/diagnosis-mode`);if(String(state.currentTaskId)===String(id)&&pending.data)acceptDiagnosisPause(pending.data);}
  if(["diagnosis.mode.selected","diagnosis.mode.expired","diagnosis.mode.superseded"].includes(parsed.event)){if(parsed.data.confirmation_id)state.settledDiagnosisIds.add(parsed.data.confirmation_id);state.diagnosisConfirmation=null;updateGeneratingUi();}
  const assistant=messageById(state.currentAssistantId);
  if(parsed.event==="answer.completed"&&assistant){assistant.status=parsed.data.status||"COMPLETED";scheduleExecutionUiRefresh();}
  if(parsed.event==="task.failed"&&assistant){assistant.status="FAILED";showStreamNotice(parsed.data.message||"任务执行失败",true);renderMessages();}
  if(parsed.event==="task.stopped"&&assistant){assistant.status="STOPPED";renderMessages();}
  if(parsed.event==="entity.selection.required"){
    if(assistant)assistant.status="WAITING_SELECTION";
    if(parsed.data.last_event_id)state.streamLastEventId=String(parsed.data.last_event_id);
    showEntitySelection({...parsed.data,task_id:parsed.data.task_id||state.currentTaskId,last_event_id:parsed.data.last_event_id||parsed.id});
    renderMessages();
  }
  if(parsed.event==="entity.selection.completed"){closeModal("entityModal");state.entityCandidates=[];state.entitySelectionTaskId=null;state.entitySelectionExpiresAt=null;if(assistant)assistant.status="STREAMING";renderMessages();}
  if(TERMINAL_EVENTS.has(parsed.event)){state.terminalReceived=true;}
}
function parseSseBlocksFromBuffer(buffer){const normalized=buffer.replace(/\r\n/g,"\n").replace(/\r/g,"\n");const blocks=[];let rest=normalized;let index;while((index=rest.indexOf("\n\n"))>=0){blocks.push(rest.slice(0,index));rest=rest.slice(index+2);}return{blocks,rest};}
async function safeHandleStreamEvent(parsed){try{await handleStreamEvent(parsed);}catch(error){state.lastStreamError=String(error?.message||error);console.error("chat_stream_event_apply_failed",{parsed,error});showStreamNotice(`收到实时事件但页面处理失败，已由持久化通道继续补偿：${state.lastStreamError}`,true);}}
async function readSseResponse(response){if(!response.body)throw new Error("SSE response body unavailable");const reader=response.body.getReader();const decoder=new TextDecoder();let buffer="";while(true){const{done,value}=await reader.read();if(done){buffer+=decoder.decode();const parsedTail=parseSseBlocksFromBuffer(buffer);for(const block of parsedTail.blocks){if(block.trim())await safeHandleStreamEvent(parseSseBlock(block));}if(parsedTail.rest.trim()){const tail=parseSseBlock(parsedTail.rest);if(tail.event!=="message"||Object.keys(tail.data||{}).length)await safeHandleStreamEvent(tail);}break;}buffer+=decoder.decode(value,{stream:true});const parsed=parseSseBlocksFromBuffer(buffer);buffer=parsed.rest;for(const block of parsed.blocks){if(block.trim())await safeHandleStreamEvent(parseSseBlock(block));}}}
function advanceStreamCursorFromPersistedRow(row){const seq=Number(row?.sequence_no);if(!Number.isFinite(seq)||seq<=0)return;const current=Number(String(state.streamLastEventId||"0-0").split("-",1)[0])||0;if(seq>current)state.streamLastEventId=`${seq}-0`;}
async function syncTaskExecutionEvents(taskId,assistantId){try{let task=null;let more=true;while(more){const after=state.persistenceCursors.get(String(taskId))||0;const result=await apiRequest(`/chat/v1/tasks/${taskId}/execution-events`,{query:{include_reasoning:true,after_sequence:after,limit:1000}});const data=result.data||{};task=data.task||task;for(const row of data.events||[]){const payload={...eventPayload(row),task_id:row.task_id||taskId,message_id:row.message_id||assistantId,sequence_no:row.sequence_no,status:row.status||eventPayload(row).status};await handleStreamEvent({id:`${row.sequence_no}-0`,event:row.event_type,data:payload,fromHistory:true});}state.persistenceCursors.set(String(taskId),data.next_after_sequence||after);more=Boolean(data.has_more);}state.persistenceSyncCount+=1;return task;}catch(error){console.debug("execution_event_sync_failed",error);return null;}}
async function taskPersistenceReconciler(taskId,assistantId,epoch){while(state.isGenerating&&state.streamEpoch===epoch&&String(state.currentTaskId)===String(taskId)){await new Promise(resolve=>setTimeout(resolve,1000));if(!state.isGenerating||state.streamEpoch!==epoch)break;const task=await syncTaskExecutionEvents(taskId,assistantId)||await taskView(taskId).catch(()=>null);if(!task||state.streamEpoch!==epoch)continue;if(task.status==="WAITING_CONFIRMATION"){const result=await apiRequest(`/chat/v1/tasks/${taskId}/diagnosis-mode`);if(state.streamEpoch===epoch&&result.data)acceptDiagnosisPause(result.data);continue;}if(task.status==="WAITING_SELECTION"){await recoverEntitySelection(taskId);continue;}if(state.terminalReceived||TERMINAL_STATUSES.has(task.status)){state.terminalReceived=true;const assistant=messageById(assistantId);if(assistant)assistant.status=task.status;state.streamAbort?.abort();scheduleExecutionUiRefresh();break;}}}
window.__CHAT_STREAM_DIAGNOSTICS__=()=>({build:CHAT_UI_BUILD,taskId:state.currentTaskId,assistantId:state.currentAssistantId,isGenerating:state.isGenerating,terminalReceived:state.terminalReceived,lastEventId:state.streamLastEventId,lastStreamRequestUrl:state.lastStreamRequestUrl,lastStreamError:state.lastStreamError,persistenceSyncCount:state.persistenceSyncCount,lastTransportEventAt:state.lastStreamEventAt,lastBusinessEventAt:state.lastBusinessEventAt});
async function taskView(taskId){return (await apiRequest(`/chat/v1/tasks/${taskId}`)).data;}
async function streamTask(taskId,assistantId,resetLast=false){
  state.streamAbort?.abort();
  const epoch=++state.streamEpoch;
  state.isGenerating=true;state.currentTaskId=taskId;state.currentAssistantId=assistantId;if(resetLast){state.streamLastEventId="0-0";state.appliedContentEventIds.clear();}state.streamAbort=new AbortController();state.lastStreamEventAt=Date.now();state.lastBusinessEventAt=Date.now();updateGeneratingUi();$("taskBadge").classList.remove("hidden");$("taskBadge").textContent=String(taskId).slice(0,8);hideStreamNotice();
  let reconnects=0;const persistenceReconciler=taskPersistenceReconciler(taskId,assistantId,epoch);
  try{
    while(!state.terminalReceived&&!state.diagnosisConfirmation&&state.streamEpoch===epoch&&reconnects<8){
      try{
        const streamUrl=taskStreamUrl(taskId);state.lastStreamRequestUrl=streamUrl;state.lastStreamError=null;console.info("chat_sse_open",{taskId,url:streamUrl,lastEventId:state.streamLastEventId,build:CHAT_UI_BUILD});
        const response=await fetch(streamUrl,{headers:{"Last-Event-ID":state.streamLastEventId,"Accept":"text/event-stream","X-Chat-UI-Build":CHAT_UI_BUILD},cache:"no-store",signal:state.streamAbort.signal});
        if(!response.ok)throw new Error(`SSE HTTP ${response.status}`);
        await readSseResponse(response);
        if(state.terminalReceived||state.diagnosisConfirmation||state.streamEpoch!==epoch)break;
        reconnects+=1;showStreamNotice(`实时事件流提前结束，正在续传（${reconnects}/8，游标 ${state.streamLastEventId}）`,true);
      }catch(error){
        if(error.name==="AbortError")break;
        state.lastStreamError=String(error?.message||error);reconnects+=1;showStreamNotice(`实时事件流传输异常，正在续传（${reconnects}/8）：${state.lastStreamError}`,true);
      }
      const task=await taskView(taskId).catch(()=>null);
      if(task?.status==="WAITING_CONFIRMATION"){const r=await apiRequest(`/chat/v1/tasks/${taskId}/diagnosis-mode`);if(r.data)acceptDiagnosisPause(r.data);break;}
      if(task?.status==="WAITING_SELECTION"){await recoverEntitySelection(taskId);}
      else if(task&&TERMINAL_STATUSES.has(task.status)){state.terminalReceived=true;const assistant=messageById(assistantId);if(assistant)assistant.status=task.status;break;}
      if(!state.terminalReceived)await new Promise(resolve=>setTimeout(resolve,Math.min(500*reconnects,2500)));
    }
  }finally{if(state.streamEpoch===epoch){state.isGenerating=false;state.streamAbort=null;updateGeneratingUi();await persistenceReconciler.catch(()=>{});if(state.streamEpoch===epoch&&!state.diagnosisConfirmation){await syncTaskExecutionEvents(taskId,assistantId);if(state.streamEpoch===epoch&&!state.diagnosisConfirmation)await loadConversationAfterTask();}hideStreamNotice();}}
}
async function loadConversationAfterTask(){if(!state.currentConversationId)return;await Promise.all([selectConversation(state.currentConversationId),loadConversations()]);}
async function stopGeneration(){if(!state.currentTaskId)return;try{await apiRequest(`/chat/v1/tasks/${state.currentTaskId}/stop`,{method:"POST"});toast("已请求停止","success");}catch(error){toast(`停止失败：${error.message}`,"error");}}

function candidateField(item,keys){const metadata=item?.metadata&&typeof item.metadata==="object"?item.metadata:{};for(const key of keys){const direct=item?.[key];if(direct!==null&&direct!==undefined&&String(direct).trim())return String(direct).trim();const nested=metadata[key];if(nested!==null&&nested!==undefined&&String(nested).trim())return String(nested).trim();}return "";}
function uniqueParts(values){const seen=new Set();return values.map(value=>String(value||"").trim()).filter(value=>value&&!seen.has(value)&&seen.add(value));}
function normalizeSpacePath(value){return uniqueParts(String(value||"").split(/[\/＞>]+/)).join(" / ");}
function candidateAreaPath(item){const explicit=candidateField(item,["space_path"]);if(explicit)return normalizeSpacePath(explicit);return uniqueParts([candidateField(item,["group_name"]),candidateField(item,["company_name"]),candidateField(item,["plant_name","factory_name","site_name"]),candidateField(item,["region_name","workshop_name"]),candidateField(item,["line_name","production_line"]),candidateField(item,["area_name","leaf_space_name"])]).join(" / ");}
function entityTypeLabel(type){return({area:"区域",space:"区域",equipment:"设备",equip:"设备",point:"测点"})[String(type||"").toLowerCase()]||"候选实体";}
function renderCandidateRow(label,name,code=""){if(!name&&!code)return "";return `<div class="entity-detail-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(name||"-")}${code?`<code>${escapeHtml(code)}</code>`:""}</strong></div>`;}
function renderEntityCandidates(){const box=$("entityCandidates");if(!box)return;box.innerHTML=state.entityCandidates.length?state.entityCandidates.map(item=>{const type=String(item.entity_type||item.type||"").toLowerCase();const areaPath=candidateAreaPath(item);const equipName=candidateField(item,["equip_name","equipment_name"]);const equipNo=candidateField(item,["equip_no","equipment_no"]);const pointName=candidateField(item,["point_name"]);const pointNo=candidateField(item,["point_no"]);const component=uniqueParts([candidateField(item,["station_name","component_name","part_name"]),candidateField(item,["position_name","install_position"]),candidateField(item,["direction_name","direction"]),candidateField(item,["param_name","metric_name","measurement_name"])]).join(" · ");const title=type==="point"?(pointName||pointNo):["equipment","equip"].includes(type)?(equipName||equipNo):(areaPath||candidateField(item,["display_name","area_name"])||item.candidate_id);const score=item.similarity??item.score;return `<button type="button" class="entity-option entity-option-full" data-candidate="${escapeHtml(item.candidate_id)}"><div class="entity-option-main"><div class="entity-option-heading"><span class="entity-type-badge">${escapeHtml(entityTypeLabel(type))}</span><strong>${escapeHtml(title)}</strong></div><div class="entity-detail-grid">${renderCandidateRow("区域",areaPath)}${renderCandidateRow("设备",equipName,equipNo)}${renderCandidateRow("测点",pointName,pointNo)}${renderCandidateRow("部件/位置",component)}</div></div><div class="entity-score"><span>${score!==null&&score!==undefined?Number(score).toFixed(3):"选择"}</span><small>匹配度</small></div></button>`;}).join(""):'<div class="trace-empty">没有可用候选实体</div>';}
function showEntitySelection(payload){const candidates=Array.isArray(payload?.candidates)?payload.candidates:[];if(!candidates.length)return;state.entityCandidates=candidates;state.entitySelectionTaskId=String(payload.task_id||state.currentTaskId||"");state.currentTaskId=state.entitySelectionTaskId||state.currentTaskId;const exactAssistantId=String(payload.assistant_message_id||payload.message_id||"");if(exactAssistantId&&messageById(exactAssistantId))state.currentAssistantId=exactAssistantId;state.entitySelectionExpiresAt=payload.expires_at||null;if(payload.last_event_id)state.streamLastEventId=String(payload.last_event_id);const expiry=$("entitySelectionExpiry");if(expiry){expiry.textContent=payload.expires_in_seconds?`请在 ${Math.max(1,Math.ceil(Number(payload.expires_in_seconds)/60))} 分钟内完成选择`:(payload.expires_at?`候选有效期至 ${formatTime(payload.expires_at)}`:"请选择一个候选实体");}renderEntityCandidates();openModal("entityModal");}
async function recoverEntitySelection(taskId){if(!taskId)return false;try{const result=await apiRequest(`/chat/v1/tasks/${taskId}/entity-selection`);showEntitySelection(result.data||{});return true;}catch(error){if(![404,409].includes(Number(error.status||0)))console.debug("entity_selection_recovery_failed",error);return false;}}
async function recoverConversationEntitySelection(conversationId){if(!conversationId)return false;try{const result=await apiRequest(`/chat/v1/conversations/${conversationId}/pending-entity-selection`);const payload=result.data;if(!payload||!Array.isArray(payload.candidates)||!payload.candidates.length)return false;showEntitySelection(payload);const taskId=payload.task_id;if(taskId){state.currentTaskId=String(taskId);const exactId=String(payload.assistant_message_id||"");const assistant=(exactId&&messageById(exactId))||[...state.messages].reverse().find(item=>normalizeMessageRole(item.role)==="ASSISTANT"&&["PENDING","WAITING_SELECTION","STREAMING"].includes(String(item.status||"")));if(assistant){assistant.status="WAITING_SELECTION";state.currentAssistantId=assistant.id;}renderMessages();}return true;}catch(error){if(![404,409].includes(Number(error.status||0)))console.debug("conversation_entity_selection_recovery_failed",error);return false;}}
async function chooseEntity(candidateId){const taskId=state.entitySelectionTaskId||state.currentTaskId;if(!taskId||!candidateId)return;const buttons=[...document.querySelectorAll("[data-candidate]")];buttons.forEach(button=>button.disabled=true);const streamAlreadyActive=state.isGenerating&&state.streamAbort&&String(state.currentTaskId)===String(taskId);try{await apiRequest(`/chat/v1/tasks/${taskId}/entity-selection`,{method:"POST",body:{candidate_id:candidateId}});closeModal("entityModal");state.entityCandidates=[];state.entitySelectionTaskId=null;state.entitySelectionExpiresAt=null;state.terminalReceived=false;const assistant=messageById(state.currentAssistantId);if(assistant)assistant.status="STREAMING";renderMessages();toast("已确认目标实体，原任务继续执行","success");if(!streamAlreadyActive)await streamTask(taskId,state.currentAssistantId,false);}catch(error){buttons.forEach(button=>button.disabled=false);toast(`实体选择失败：${error.message}`,"error");}}

async function editMessage(id){const message=messageById(id);if(!message)return;const content=prompt("修改用户问题",message.content);if(content===null||!content.trim())return;try{const result=await apiRequest(`/chat/v1/messages/${id}/edit-and-resubmit`,{method:"POST",generation:true,body:{content:content.trim(),attachments:null,execution_mode:executionMode()}});const accepted=result.data;appendLocalMessage({id:accepted.user_message_id,role:"USER",content:content.trim(),status:"COMPLETED",created_at:new Date().toISOString(),metadata_json:{execution_mode:accepted.execution_mode},alternative_count:1,alternative_index:1});appendLocalMessage({id:accepted.assistant_message_id,role:"ASSISTANT",content:"",status:"STREAMING",created_at:new Date().toISOString(),metadata_json:{execution_mode:accepted.execution_mode},alternative_count:1,alternative_index:1});state.messageEvents.set(String(accepted.assistant_message_id),[]);state.currentStreamUrl=accepted.agent_stream_url||accepted.stream_url||null;state.streamLastEventId="0-0";state.terminalReceived=false;state.appliedContentEventIds.clear();await streamTask(accepted.task_id,accepted.assistant_message_id,true);}catch(error){toast(`编辑重提失败：${error.message}`,"error");}}
async function regenerateMessage(id){try{const result=await apiRequest(`/chat/v1/messages/${id}/regenerate`,{method:"POST",generation:true,body:{force_reresolve:false,execution_mode:executionMode()}});const accepted=result.data;appendLocalMessage({id:accepted.assistant_message_id,role:"ASSISTANT",content:"",status:"STREAMING",created_at:new Date().toISOString(),metadata_json:{execution_mode:accepted.execution_mode},alternative_count:1,alternative_index:1});state.messageEvents.set(String(accepted.assistant_message_id),[]);state.currentStreamUrl=accepted.agent_stream_url||accepted.stream_url||null;state.streamLastEventId="0-0";state.terminalReceived=false;state.appliedContentEventIds.clear();await streamTask(accepted.task_id,accepted.assistant_message_id,true);}catch(error){toast(`重新生成失败：${error.message}`,"error");}}
async function switchAlternative(id,direction){try{const result=await apiRequest(`/chat/v1/messages/${id}/alternatives`);const rows=result.data||[];if(rows.length<2)return;const index=Math.max(0,rows.findIndex(item=>String(item.id)===String(id)));const target=rows[(index+direction+rows.length)%rows.length];await apiRequest(`/chat/v1/conversations/${state.currentConversationId}/branches/${target.branch_id}/activate`,{method:"POST"});await selectConversation(state.currentConversationId);}catch(error){toast(`版本切换失败：${error.message}`,"error");}}
async function renameConversation(){if(!state.currentConversationId)return;const title=prompt("新的会话标题",$("conversationTitle").textContent);if(!title?.trim())return;try{const result=await apiRequest(`/chat/v1/conversations/${state.currentConversationId}`,{method:"PATCH",body:{title:title.trim()}});state.currentConversation={...(state.currentConversation||{}),...result.data};$("conversationTitle").textContent=result.data.title;await loadConversations();}catch(error){toast(`重命名失败：${error.message}`,"error");}}
async function togglePin(){if(!state.currentConversationId)return;try{const result=await apiRequest(`/chat/v1/conversations/${state.currentConversationId}/pin`,{method:state.currentConversation?.is_pinned?"DELETE":"POST"});state.currentConversation={...(state.currentConversation||{}),...result.data};updateHeaderActions();await loadConversations();}catch(error){toast(`置顶操作失败：${error.message}`,"error");}}
async function deleteConversationById(id,title="当前会话"){if(!id||!confirm(`确认删除“${title}”？运行中的任务也会停止。`))return;try{await apiRequest(`/chat/v1/conversations/${id}`,{method:"DELETE"});if(String(state.currentConversationId)===String(id))newChat();await loadConversations();toast("会话已删除","success");}catch(error){toast(`删除失败：${error.message}`,"error");}}
async function deleteConversation(){if(state.currentConversationId)await deleteConversationById(state.currentConversationId,$("conversationTitle").textContent);}
async function loadProfile(){try{const result=await apiRequest("/chat/v1/profile");$("profileJson").value=JSON.stringify(result.data.profile_json||{},null,2);}catch(error){toast(`读取画像失败：${error.message}`,"error");}}
async function saveProfile(){try{const profile=JSON.parse($("profileJson").value||"{}");await apiRequest("/chat/v1/profile",{method:"PATCH",body:{profile_json:profile}});toast("画像已保存","success");}catch(error){toast(`保存画像失败：${error.message}`,"error");}}
async function loadSuggestions(){const defaults=["查看酸轧车间设备健康度","查询1号风机驱动端温度趋势","分析当前报警设备的主要风险","解释振动速度有效值的含义"];let rows=defaults;if(userToken())try{const result=await apiRequest("/chat/v1/suggested-queries",{query:{app_code:appCode(),limit:8}});if(Array.isArray(result.data)&&result.data.length)rows=[...result.data,...defaults].slice(0,8);}catch(_){}$("suggestionGrid").innerHTML=rows.slice(0,4).map(text=>`<button class="suggestion" data-suggestion="${escapeHtml(text)}">${escapeHtml(text)}</button>`).join("");}

function bindEvents(){
  $("newChatBtn").onclick=newChat;$("conversationSearch").oninput=renderConversations;$("settingsBtn").onclick=()=>openModal("settingsModal");
  $("saveSettingsBtn").onclick=()=>{saveLocalSettings();closeModal("settingsModal");updateComposerHint();testConnection(false);loadConversations();loadSuggestions();};$("testConnectionBtn").onclick=()=>testConnection(true);$("loadProfileBtn").onclick=loadProfile;$("saveProfileBtn").onclick=saveProfile;
  $("messageInput").oninput=autoResizeInput;$("messageInput").onkeydown=event=>{if(event.key==="Enter"&&!event.shiftKey){event.preventDefault();sendMessage();}};$("sendBtn").onclick=()=>sendMessage();$("stopBtn").onclick=stopGeneration;$("micBtn").onclick=toggleAsrRecording;$("attachBtn").onclick=()=>$("attachmentInput").click();$("attachmentInput").onchange=event=>handleAttachmentFiles(event.target.files);
  $("quickModeBtn").onclick=()=>setExecutionMode("quick");$("normalModeBtn").onclick=()=>setExecutionMode("normal");$("expertModeBtn").onclick=()=>setExecutionMode("expert");
  $("renameBtn").onclick=renameConversation;$("pinBtn").onclick=togglePin;$("deleteBtn").onclick=deleteConversation;$("traceToggleBtn").onclick=()=>document.querySelector(".chat-body").classList.toggle("trace-open");$("closeTraceBtn").onclick=()=>document.querySelector(".chat-body").classList.remove("trace-open");$("copyTraceBtn").onclick=()=>copyPlainText(pretty(state.traceEvents),"执行过程已复制");$("clearTraceBtn").onclick=()=>{state.traceEvents=[];renderTrace();};$("mobileMenuBtn").onclick=()=>document.querySelector(".chat-sidebar").classList.toggle("open");
  document.addEventListener("click",event=>{const diagnosis=event.target.closest("[data-diagnosis-mode]");if(diagnosis){chooseDiagnosisMode(diagnosis.dataset.diagnosisMode);return;}const deletion=event.target.closest("[data-delete-conversation]");if(deletion){event.stopPropagation();deleteConversationById(deletion.dataset.deleteConversation,deletion.dataset.deleteTitle);return;}const conversation=event.target.closest("[data-conversation]");if(conversation)selectConversation(conversation.dataset.conversation);const suggestion=event.target.closest("[data-suggestion]");if(suggestion)sendMessage(suggestion.dataset.suggestion);const remove=event.target.closest("[data-remove-attachment]");if(remove)removePendingAttachment(remove.dataset.removeAttachment);const copy=event.target.closest("[data-copy-message]");if(copy){const message=messageById(copy.dataset.copyMessage);if(message)copyPlainText(message.content,"消息已复制");}const edit=event.target.closest("[data-edit-message]");if(edit)editMessage(edit.dataset.editMessage);const regenerate=event.target.closest("[data-regenerate]");if(regenerate)regenerateMessage(regenerate.dataset.regenerate);const previous=event.target.closest("[data-alt-prev]");if(previous)switchAlternative(previous.dataset.altPrev,-1);const next=event.target.closest("[data-alt-next]");if(next)switchAlternative(next.dataset.altNext,1);const candidate=event.target.closest("[data-candidate]");if(candidate){chooseEntity(candidate.dataset.candidate);return;}const close=event.target.closest("[data-close-modal]");if(close)closeModal(close.dataset.closeModal);});
  for(const id of ["apiBase","userToken","dataToken","appCode"])$(id).oninput=updateComposerHint;
}
async function init(){console.info("Chat UI build 1.0.0.12");loadLocalSettings();bindEvents();setExecutionMode(state.executionMode);newChat();await testConnection(false);await Promise.all([loadConversations(),loadSuggestions()]);updateHeaderActions();renderAsrUi();}
document.addEventListener("DOMContentLoaded",init);

document.addEventListener("toggle",event=>{const id=event.target?.dataset?.reasoningMessage;if(id){const out=outputFor(id);if(out)out.folded=!event.target.open;}},true);
setInterval(()=>{if(state.diagnosisConfirmation&&Date.parse(state.diagnosisConfirmation.expires_at)<=Date.now())renderMessages();},1000);
