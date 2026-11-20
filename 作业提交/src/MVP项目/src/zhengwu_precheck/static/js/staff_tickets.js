/* 工单详情表单渐进增强；无脚本时仍可提交并显示业务错误。 */
(() => {
  const dialog = document.getElementById('ticket-error-dialog');
  if (!dialog) return;
  const message = document.getElementById('ticket-error-message');
  const forms = [...document.querySelectorAll('form[action^="/staff/api/tickets/"]')];
  let busy = false;
  let refreshOnClose = false;
  let lastSubmitter = null;

  const showError = (text, refresh = false) => {
    message.textContent = text;
    refreshOnClose = refresh;
    if (!dialog.open) dialog.showModal();
  };
  dialog.addEventListener('close', () => {
    if (refreshOnClose) window.location.assign(dialog.dataset.ticketUrl || '/staff/tickets');
    else lastSubmitter?.focus();
  });
  if (dialog.open) {
    dialog.close();
    dialog.showModal();
  }

  for (const form of forms) {
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (busy || form.querySelector('fieldset:disabled')) return;
      const data = Object.fromEntries(new FormData(form));
      const csrf = data.csrf_token;
      delete data.csrf_token;
      data.expected_version = Number(data.expected_version);
      lastSubmitter = event.submitter;
      const buttons = forms.flatMap(item => [...item.querySelectorAll('button')]);
      const disabled = buttons.map(button => button.disabled);
      busy = true;
      let succeeded = false;
      buttons.forEach(button => { button.disabled = true; });
      form.setAttribute('aria-busy', 'true');
      try {
        const response = await fetch(form.action, {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
          body: JSON.stringify(data)
        });
        if (response.ok) {
          succeeded = true;
          window.location.assign(dialog.dataset.ticketUrl);
          return;
        }
        const result = await response.json();
        const error = result.error || {};
        showError(error.message || '操作未完成，请刷新工单后重试。',
          ['TRANSFER_PENDING', 'TICKET_CONFLICT', 'TICKET_CLOSED'].includes(error.code));
      } catch {
        showError('暂时无法确认操作结果，请刷新工单核对最新状态后再操作。', true);
      } finally {
        if (!succeeded) {
          busy = false;
          buttons.forEach((button, index) => { button.disabled = disabled[index]; });
          form.removeAttribute('aria-busy');
        }
      }
    });
  }
})();
