/* 表单保留原生POST后备；一次提交沿用服务器生成的幂等键，失败重试不换键。 */
const status = document.getElementById('ticket-status');
const csrf = document.querySelector('meta[name="csrf-token"]').content;
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
      location.assign('/my/tickets' + (ticketId ? '?ticket_id=' + encodeURIComponent(ticketId) : ''));
    } catch (error) {
      status.textContent = error.message + '；如提示版本冲突，请刷新页面后核对工单状态。';
      busy = false;
      buttons.forEach(button => { button.disabled = false; });
    }
  });
}
// 仅辅助匹配教学情形；无脚本仍可手动选择，服务器再次验证事项与情形。
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
