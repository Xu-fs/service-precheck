/* 表单保留原生POST后备；一次提交沿用服务器生成的幂等键，失败重试不换键。 */
const status = document.getElementById('ticket-status');
const csrf = document.querySelector('meta[name="csrf-token"]').content;
// 宽屏使用既有侧栏；窄屏收为浮动入口，展开时覆盖显示，不挤动主区域。
const examplePanel = document.getElementById('ticket-examples');
if (examplePanel) {
  const narrowScreen = matchMedia('(max-width:760px)');
  const arrangeExamples = () => { examplePanel.open = !narrowScreen.matches; };
  arrangeExamples();
  narrowScreen.addEventListener('change', arrangeExamples);
  document.addEventListener('pointerdown', event => {
    if (narrowScreen.matches && examplePanel.open && !examplePanel.contains(event.target)) examplePanel.open = false;
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && narrowScreen.matches && examplePanel.open) {
      examplePanel.open = false;
      examplePanel.querySelector('summary').focus();
    }
  });
}
let busy = false;
for (const form of document.querySelectorAll('form[data-ticket-write]')) {
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    busy = true;
    const buttons = [...document.querySelectorAll('form[data-ticket-write] button')];
    buttons.forEach(button => { button.disabled = true; });
    status.textContent = '正在提交，请稍候……';
    const data = Object.fromEntries(new FormData(form));
    delete data.csrf_token;
    if ('expected_version' in data) data.expected_version = Number(data.expected_version);
    try {
      const response = await fetch(form.action, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify(data)});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error?.message || '提交失败，请稍后重试');
      const ticketId = result.id?.startsWith('TK-') ? result.id : new URL(location.href).searchParams.get('ticket_id');
      const nextPage = '/my/tickets' + (ticketId ? '?ticket_id=' + encodeURIComponent(ticketId) : '');
      // 当前详情中的补充、已读等操作必须重新读取服务端状态；只改锚点不会刷新页面。
      if (location.pathname + location.search === nextPage) location.reload();
      else location.assign(nextPage + (ticketId ? '#ticket-detail' : ''));
    } catch (error) {
      status.textContent = error.message + '；如提示版本冲突，请刷新页面后核对工单状态。';
      busy = false;
      buttons.forEach(button => { button.disabled = false; });
    }
  });
}
// 仅辅助匹配申请情形；无脚本仍可手动选择，服务器再次验证事项与情形。
const matter = document.getElementById('matter');
const scenario = document.getElementById('scenario');
if (matter && scenario) {
  const update = () => {
    for (const option of scenario.options) option.hidden = option.dataset.matter !== matter.value;
    if (scenario.selectedOptions[0]?.hidden) scenario.value = [...scenario.options].find(option => !option.hidden)?.value || '';
  };
  matter.addEventListener('change', update);
  update();
}
