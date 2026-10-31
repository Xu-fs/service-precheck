const root = document.getElementById('workflow-demo');
const play = document.getElementById('demo-play');
if (play) {
  const pause = document.getElementById('demo-pause');
  const next = document.getElementById('demo-next');
  const fresh = document.getElementById('demo-new');
  const preset = document.getElementById('demo-preset');
  const speed = document.getElementById('demo-speed');
  const status = document.getElementById('demo-status');
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  let state = null;
  let playing = false;
  let playGeneration = 0;
  let busy = false;
  let startKey = crypto.randomUUID();
  const done = () => state && state.completed_steps === state.total_steps;
  const element = (tag, text) => {
    const node = document.createElement(tag);
    node.textContent = text;
    return node;
  };

  function controls() {
    play.disabled = busy || playing || !!done();
    play.textContent = state ? '继续自动演示' : '开始自动演示';
    pause.disabled = !playing;
    next.disabled = busy || playing || !!done();
    fresh.disabled = busy || playing;
    preset.disabled = busy || playing;
  }

  async function request(path, body) {
    const options = body === undefined ? {} : {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
      body: JSON.stringify(body),
    };
    const response = await fetch('/examples/api/workflows' + path, {...options, credentials: 'same-origin'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || '示例暂时无法执行，请稍后重试');
    return data;
  }

  function paint() {
    if (!state) return;
    preset.value = state.preset_id;
    document.getElementById('demo-title').textContent = state.title;
    document.getElementById('demo-role').textContent = state.view_name;
    document.getElementById('demo-stage').dataset.role = state.view_role;
    document.getElementById('demo-count').textContent = `${state.completed_steps} / ${state.total_steps} 步${done() ? ' · 已结案' : ''}`;
    document.getElementById('demo-progress').value = state.completed_steps;
    const last = state.steps[state.completed_steps - 1];
    document.getElementById('demo-operation').textContent = last ? last.label : '等待办事人发送问题';
    document.getElementById('demo-result').textContent = done() ? '办事人已确认问题解决，本轮示例已结案。完整记录已保留。' : last?.text || state.ticket.content;
    const steps = document.getElementById('demo-steps');
    steps.replaceChildren(...state.steps.map((step, index) => {
      const item = element('li', step.label);
      item.append(element('small', step.role_name));
      item.dataset.state = index < state.completed_steps ? 'done' : index === state.completed_steps ? 'active' : 'waiting';
      if (index === state.completed_steps) item.setAttribute('aria-current', 'step');
      return item;
    }));
    const messages = document.getElementById('demo-messages');
    messages.replaceChildren(...state.messages.map(message => {
      const article = document.createElement('article');
      article.append(element('strong', message.role_name), element('p', message.content));
      return article;
    }));
    if (!state.messages.length) messages.append(element('p', '等待办事人发送第一条消息。'));
    const transfers = document.getElementById('demo-transfers');
    transfers.replaceChildren(...state.transfers.map(transfer => {
      const target = transfer.target_role === 'reviewer' ? '业务审核员' : '服务中心主任';
      const item = element('li', `提交至${target} · ${transfer.status === 'accepted' ? '已确认接收' : '待接收'}`);
      item.append(element('p', transfer.reason));
      return item;
    }));
    if (!state.transfers.length) transfers.append(element('li', '本轮尚无转派。'));
    status.textContent = done() ? '本轮示例已结案。可以新建一轮，或返回手动逐步办理。' : `下一步：${state.next_label}`;
    controls();
  }

  async function history() {
    const rows = await request('');
    const list = document.getElementById('demo-history');
    list.replaceChildren(...rows.map(row => {
      const item = document.createElement('li');
      const link = element('a', `${row.title} · ${row.status === 'closed' ? '已结案' : '进行中'}`);
      link.href = '/examples/workflow?run=' + encodeURIComponent(row.id);
      item.append(link);
      return item;
    }));
    if (!rows.length) list.append(element('li', '尚无历史演示记录。'));
  }

  async function createRun() {
    busy = true;
    controls();
    try {
      state = await request('', {preset_id: preset.value, idempotency_key: startKey});
      const url = new URL(location.href);
      url.searchParams.set('run', state.id);
      url.searchParams.set('example', state.preset_id);
      window.history.pushState(null, '', url);
      paint();
      await history();
    } finally { busy = false; controls(); }
  }

  async function advance() {
    busy = true;
    controls();
    try {
      state = await request('/' + encodeURIComponent(state.id) + '/next', {expected_step: state.completed_steps});
      paint();
      if (done()) await history();
    } finally { busy = false; controls(); }
  }

  function failed(error) {
    playing = false;
    status.textContent = error.message + '。已保存的步骤会保留，可刷新或继续重试。';
    controls();
  }

  play.addEventListener('click', async () => {
    if (busy || playing || done()) return;
    const generation = ++playGeneration;
    playing = true;
    controls();
    try {
      if (!state) await createRun();
      while (playing && generation === playGeneration && !done()) {
        await advance();
        if (playing && generation === playGeneration && !done()) await new Promise(resolve => setTimeout(resolve, Number(speed.value)));
      }
      if (generation === playGeneration && !done()) status.textContent = '已暂停。下一步：' + state.next_label;
    } catch (error) { failed(error); }
    finally { if (generation === playGeneration) { playing = false; controls(); } }
  });

  pause.addEventListener('click', () => {
    playing = false;
    playGeneration += 1;
    status.textContent = busy ? '正在保存当前一步，随后暂停。' : '已暂停。可点击“下一步”或继续自动演示。';
    controls();
  });

  next.addEventListener('click', async () => {
    if (busy || playing || done()) return;
    try { if (!state) await createRun(); await advance(); }
    catch (error) { failed(error); }
  });

  fresh.addEventListener('click', async () => {
    if (busy || playing) return;
    startKey = crypto.randomUUID();
    try { await createRun(); }
    catch (error) { failed(error); }
  });

  preset.addEventListener('change', () => {
    state = null;
    startKey = crypto.randomUUID();
    const url = new URL(location.href);
    url.searchParams.delete('run');
    url.searchParams.set('example', preset.value);
    // 新案例先显示准备界面，防止旧记录被误认为是刚选中的案例。
    location.assign(url.href);
  });

  window.addEventListener('popstate', () => location.reload());
  async function load() {
    busy = true;
    controls();
    try {
      if (root.dataset.runId) {
        state = await request('/' + encodeURIComponent(root.dataset.runId));
        paint();
      }
      await history();
    } catch (error) { failed(error); }
    finally { busy = false; controls(); }
  }
  load();
}
