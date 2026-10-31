/* 只保存会话ID，身份凭证由HttpOnly Cookie管理；所有模型/用户文本用textContent。 */
import { readSSE } from './sse.js';
import { createNotifications } from './notifications.js';

const $ = id => document.getElementById(id);
let csrf = document.querySelector('meta[name="csrf-token"]').content;
let session = null, identityKey = 'anonymous', busy = false, controller = null, nextCursor = null;
let pendingQuestion = '', generation = 0;
const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const storageKey = () => `zhengwu.chat.${identityKey}`;
function storeSession(value) { session = value; try { value ? localStorage.setItem(storageKey(), value) : localStorage.removeItem(storageKey()); } catch {} }
function scrollBottom() { $('scroll-area').scrollTop = $('scroll-area').scrollHeight; }
function status(text = '') { $('global-status').textContent = text; }
function setBusy(value) { busy = value; $('send').disabled = value; $('stop').hidden = !value; $('new-chat').disabled = value; $('change-matter').disabled = value; $('login-open').disabled = value; $('logout').disabled = value; }

async function jsonRequest(path, options = {}) {
  const response = await fetch(path, { credentials: 'same-origin', ...options, headers: {
    'Content-Type': 'application/json', 'X-CSRF-Token': csrf, ...options.headers,
  }});
  const body = await response.json();
  if (!response.ok) { const error = new Error(body.error?.message || '服务暂时不可用，请稍后重试。'); error.code = body.error?.code; throw error; }
  return body;
}

const notifications = createNotifications({ request: jsonRequest, openLogin: () => $('login-dialog').showModal() });

function message(role, text = '') {
  $('welcome').hidden = true;
  const wrapper = document.createElement('article'); wrapper.className = `message ${role}`;
  const speaker = document.createElement('div'); speaker.className = 'speaker'; speaker.textContent = role === 'user' ? '你' : '政务易办';
  const content = document.createElement('div'); content.className = 'message-content'; content.textContent = text;
  wrapper.append(speaker, content); $('messages').append(wrapper); scrollBottom();
  return { wrapper, content, target: text, printing: null };
}

function typeText(view, text) {
  view.target += text;
  if (reduced) { view.content.textContent = view.target; scrollBottom(); return; }
  if (!view.printing) view.printing = (async () => {
    while (view.content.textContent.length < view.target.length) {
      const length = reduced ? view.target.length : view.content.textContent.length + 3;
      view.content.textContent = view.target.slice(0, length); scrollBottom();
      if (!reduced) await sleep(10);
    }
    view.printing = null;
  })();
}

function citations(view, items) {
  if (!items?.length) return;
  const details = document.createElement('details'); details.className = 'citations';
  const summary = document.createElement('summary'); summary.textContent = `查看引用来源（${items.length}）`; details.append(summary);
  for (const item of items) {
    const block = document.createElement('div'); block.className = 'citation';
    const title = document.createElement('strong'); title.textContent = item.filename;
    const meta = document.createElement('small');
    const locator = `${item.page_no ? `第${item.page_no}页　` : ''}片段：${item.chunk_no ?? item.chunk_id}`;
    meta.textContent = `事项：${item.matter}　规则版本：${item.rule_version}　${locator}`;
    const quote = document.createElement('blockquote'); quote.textContent = item.quote;
    block.append(title, meta, quote); details.append(block);
  }
  view.wrapper.append(details);
}

function actions(view, items) {
  const group = document.createElement('div'); group.className = 'action-list';
  for (const item of items || []) {
    if (item.type === 'open_service_guide' && item.enabled) {
      const link = document.createElement('a'); link.href = item.href; link.textContent = item.label;
      group.append(link); continue;
    }
    const button = document.createElement('button'); button.type = 'button'; button.textContent = item.label; button.disabled = !item.enabled;
    button.addEventListener('click', () => {
        if (item.type === 'create_ticket') { window.location.assign('/my/tickets'); }
      if (item.type === 'clarify') { $('question').focus(); status('请补充具体事项、适用情形或想了解的要求。'); }
      if (item.type === 'human_service' && !group.querySelector('.human-info')) {
        const info = document.createElement('p'); info.className = 'human-info'; info.textContent = item.text; group.append(info);
      }
    });
    group.append(button);
  }
  view.wrapper.append(group);
}

function matterOptions(view, data, question) {
  const group = document.createElement('div'); group.className = 'action-list';
  for (const option of data.options) {
    const button = document.createElement('button'); button.type = 'button'; button.textContent = option.name;
    const small = document.createElement('small'); small.textContent = option.scenario_name; button.append(small);
    button.addEventListener('click', async () => {
      if (busy) return;
      for (const child of group.children) child.disabled = true;
      try {
        await jsonRequest(`/sessions/${session}/matter`, { method: 'POST', body: JSON.stringify({ matter_id: option.matter_id, scenario_id: option.scenario_id, confirmation_token: data.confirmation_token }) });
        $('matter-label').textContent = option.name;
        group.remove();
        const original = question || pendingQuestion;
        // “切换事项”属于界面控制意图，确认后改为询问新事项，避免重复触发选择。
        const nextQuestion = !original || /换个事项|切换事项|重新选择事项/.test(original)
          ? `请介绍${option.name}的示例材料清单。` : original;
        await sendQuestion(nextQuestion);
      } catch (error) { status(error.message); for (const child of group.children) child.disabled = false; }
    }); group.append(button);
  }
  view.wrapper.append(group); scrollBottom();
}

async function restore(older = false) {
  if (!session) return;
  const suffix = older && nextCursor ? `?before=${encodeURIComponent(nextCursor)}` : '';
  const savedTop = $('scroll-area').scrollHeight;
  const previous = older ? [...$('messages').children] : [];
  const data = await jsonRequest(`/sessions/${session}/messages${suffix}`);
  if (!older) $('messages').replaceChildren();
  else $('messages').replaceChildren();
  let lastQuestion = '';
  for (let i = 0; i < data.items.length; i++) {
    const item = data.items[i];
    if (item.sender_type === 'user') { lastQuestion = item.content; pendingQuestion = item.content; }
    const text = item.status === 'pending' ? '这条问题仍在处理中，请稍后刷新查看。' :
      item.status === 'failed' ? '上次回答未完成，可以重新提问。' : item.status === 'cancelled' ? '回答已停止。' : item.content;
    const view = message(item.sender_type, text); citations(view, item.citations);
    if (item.result?.actions?.length) actions(view, item.result.actions);
    if (!older && i === data.items.length - 1 && item.outcome === 'needs_matter_confirmation') matterOptions(view, item.result, lastQuestion);
  }
  if (older) { $('messages').append(...previous); $('scroll-area').scrollTop = $('scroll-area').scrollHeight - savedTop; }
  else scrollBottom();
  nextCursor = data.next_cursor; $('load-older').hidden = !nextCursor;
  $('matter-label').textContent = data.matter_name || '尚未确认事项';
  return data.items.some(item => item.status === 'pending');
}

async function sendQuestion(question) {
  question = question.trim(); if (!question || busy) return;
  const localGeneration = generation;
  setBusy(true); status(); pendingQuestion = question;
  $('question').value = ''; $('counter').textContent = '0 / 2000';
  message('user', question);
  const view = message('assistant');
  const progress = document.createElement('div'); progress.className = 'message-status'; progress.setAttribute('role', 'status'); progress.textContent = '正在连接咨询服务……'; view.wrapper.append(progress);
  controller = new AbortController(); let lastSeq = 0, failed = false;
  try {
    const response = await fetch('/chat', { method: 'POST', credentials: 'same-origin', signal: controller.signal,
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream', 'X-CSRF-Token': csrf, 'Idempotency-Key': crypto.randomUUID() },
      body: JSON.stringify({ session_id: session, question }) });
    if (!response.ok) { const error = await response.json(); throw new Error(error.error?.message || '请求未能完成。'); }
    if (!response.headers.get('content-type')?.includes('text/event-stream')) throw new Error('服务响应格式异常，请稍后重试。');
    await readSSE(response, async (event, data) => {
      if (data.seq !== lastSeq + 1) throw new Error('消息传输不完整，正在恢复记录。');
      lastSeq = data.seq;
      if (event === 'session') storeSession(data.session_id);
      if (event === 'status') progress.textContent = data.text;
      if (event === 'answer_delta') { progress.hidden = true; typeText(view, data.text); }
      if (event === 'citations') citations(view, data.items);
      if (event === 'actions') actions(view, data.items);
      if (event === 'matter_required') { progress.hidden = true; $('matter-label').textContent = '尚未确认事项'; view.content.textContent = data.text; matterOptions(view, data, question); }
      if (event === 'fallback') { progress.hidden = true; view.content.textContent = data.text; actions(view, data.actions); }
      if (event === 'error') { failed = true; progress.hidden = true; view.content.textContent = data.message; view.wrapper.classList.add('error'); }
      if (event === 'done' || event === 'error') { progress.hidden = true; return true; }
      scrollBottom(); return false;
    });
    if (view.printing) await view.printing;
  } catch (error) {
    failed = true; progress.hidden = true;
    view.content.textContent = error.name === 'AbortError' ? '回答已停止，正在核对已保存的记录。' : error.message;
    view.wrapper.classList.add('error');
    if (session && localGeneration === generation) {
      await sleep(250);
      try { await restore(); } catch (restoreError) { status(restoreError.message); }
    }
  } finally {
    if (localGeneration === generation) { setBusy(false); controller = null; if (!failed) $('question').focus(); scrollBottom(); }
  }
}

async function initialize() {
  notifications.setIdentity(false);
  try {
    const data = await jsonRequest('/bootstrap'); csrf = data.csrf_token;
    identityKey = data.identity === 'user' ? `user:${data.username}` : 'anonymous';
    $('identity').textContent = data.username ? `已登录：${data.username}` : '公开咨询无需登录';
    $('login-open').hidden = data.identity === 'user'; $('logout').hidden = data.identity !== 'user';
    notifications.setIdentity(data.identity === 'user');
    try { session = localStorage.getItem(storageKey()); } catch {}
    if (session) {
      try { await restore(); } catch (error) { storeSession(null); status(error.message); }
    }
  } catch (error) {
    status(error.message); if (error.code === 'AUTH_EXPIRED') { $('logout').hidden = false; $('login-open').hidden = true; }
  }
}

$('chat-form').addEventListener('submit', event => { event.preventDefault(); sendQuestion($('question').value); });
$('question').addEventListener('input', () => { $('counter').textContent = `${$('question').value.length} / 2000`; });
$('question').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); sendQuestion($('question').value); } });
for (const button of document.querySelectorAll('[data-question]')) button.addEventListener('click', () => sendQuestion(button.dataset.question));
$('stop').addEventListener('click', () => controller?.abort());
$('change-matter').addEventListener('click', () => sendQuestion('请重新选择事项。'));
$('new-chat').addEventListener('click', () => { generation++; storeSession(null); $('messages').replaceChildren(); $('welcome').hidden = false; $('load-older').hidden = true; $('matter-label').textContent = '尚未确认事项'; status(); $('question').focus(); });
$('load-older').addEventListener('click', () => restore(true).catch(error => status(error.message)));
$('login-open').addEventListener('click', () => $('login-dialog').showModal());
$('login-close').addEventListener('click', () => $('login-dialog').close());
$('login-form').addEventListener('submit', async event => {
  event.preventDefault(); $('login-error').textContent = '';
  try {
    await jsonRequest('/auth/login', { method: 'POST', body: JSON.stringify({ username: $('username').value, password: $('password').value }) });
    $('password').value = ''; $('login-dialog').close(); session = null; $('messages').replaceChildren(); $('welcome').hidden = false; $('matter-label').textContent = '尚未确认事项'; await initialize(); status('已登录。匿名咨询与个人咨询分别保存。');
  } catch (error) { $('login-error').textContent = error.message; }
});
$('logout').addEventListener('click', async () => {
  try { await jsonRequest('/auth/logout', { method: 'POST' }); session = null; $('messages').replaceChildren(); $('welcome').hidden = false; $('matter-label').textContent = '尚未确认事项'; await initialize(); status('已退出登录。'); } catch (error) { status(error.message); }
});
initialize();
