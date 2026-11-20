/* 站内消息独立刷新，不打断聊天或清空正在输入的问题。 */
export function createNotifications({ request, openLogin }) {
  const panel = document.getElementById('notification-panel');
  const list = document.getElementById('notification-items');
  const status = document.getElementById('notification-status');
  const count = document.getElementById('notification-count');
  const refreshButton = document.getElementById('notifications-refresh');
  const loginButton = document.getElementById('notifications-login');
  const narrow = matchMedia('(max-width:1100px)');
  let signedIn = false, revision = 0, loading = false, changing = false;
  let displayedItems = [];
  const arrange = () => { panel.open = !narrow.matches; };
  arrange();
  narrow.addEventListener('change', arrange);
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && narrow.matches && panel.open) {
      panel.open = false;
      panel.querySelector('summary').focus();
    }
  });
  const node = (tag, className, text) => {
    const element = document.createElement(tag);
    element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  };
  function render(items) {
    displayedItems = items;
    list.replaceChildren();
    const unread = items.filter(item => !item.read_at).length;
    count.textContent = String(unread);
    count.hidden = unread === 0;
    count.setAttribute('aria-label', `${unread} 条未读消息`);
    for (const item of items) {
      const article = node('article', 'notification-item');
      article.dataset.notificationId = item.id;
      const heading = node('div', 'notification-heading');
      heading.append(node('h3', '', item.title || '工单通知'));
      if (item.ticket_status === 'closed') heading.append(node('span', 'notification-state closed', '已办结'));
      else if (['queued', 'processing', 'waiting_citizen'].includes(item.ticket_status)) heading.append(node('span', 'notification-state', '办理中'));
      article.append(heading);
      if (item.ticket_title) article.append(node('p', 'notification-ticket-title', item.ticket_title));
      article.append(node('p', '', item.content || item.message || ''));
      if (item.ticket_close_reason === 'default_complete') article.append(node('small', 'notification-reason', '超时默认办结，不代表本人确认解决'));
      const time = node('time', 'notification-time');
      if (item.created_at) {
        const date = new Date(item.created_at * 1000);
        time.dateTime = date.toISOString();
        time.textContent = new Intl.DateTimeFormat('zh-CN', {
          timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
          hour: '2-digit', minute: '2-digit', hour12: false,
        }).format(date);
      }
      article.append(time);
      const actions = node('div', 'notification-actions');
      if (item.ticket_id) {
        const link = node('a', '', '查看工单');
        link.href = '/my/tickets?ticket_id=' + encodeURIComponent(item.ticket_id);
        actions.append(link);
      }
      if (!item.read_at) {
        const button = node('button', 'text-button notification-read', '标记已读');
        button.type = 'button';
        const operationKey = crypto.randomUUID();
        button.addEventListener('click', () => change(item, 'read', operationKey));
        actions.append(button);
      }
      const remove = node('button', 'notification-delete', '×');
      remove.type = 'button';
      remove.setAttribute('aria-label', '删除消息');
      remove.title = '删除消息';
      const deleteKey = crypto.randomUUID();
      remove.addEventListener('click', () => change(item, 'delete', deleteKey));
      actions.append(remove);
      article.append(actions);
      list.append(article);
    }
    status.textContent = items.length ? '' : '暂无消息，提交工单后可在这里查看办理动态。';
  }
  function disableActions(value) {
    refreshButton.disabled = value;
    for (const button of list.querySelectorAll('button')) button.disabled = value;
  }
  async function change(item, action, operationKey) {
    if (changing) return;
    // 作废正在返回的旧列表，避免删除成功后旧请求又把消息显示出来。
    const current = ++revision;
    changing = true;
    loading = false;
    disableActions(true);
    let succeeded = false;
    try {
      await request('/notifications/' + encodeURIComponent(item.id) + '/' + action, {
        method: 'POST', body: JSON.stringify({ expected_version: 0, idempotency_key: operationKey }),
      });
      if (current === revision) {
        succeeded = true;
        if (action === 'delete') render(displayedItems.filter(entry => entry.id !== item.id));
      }
    } catch (error) {
      if (current === revision) status.textContent = error.message;
    } finally {
      if (current === revision) {
        changing = false;
        disableActions(false);
      }
    }
    if (succeeded && current === revision) {
      if (action === 'read') await refresh();
      else (list.querySelector('.notification-delete') || refreshButton).focus();
    }
  }
  async function refresh() {
    if (!signedIn || loading || changing) return;
    const current = revision;
    loading = true;
    refreshButton.disabled = true;
    try {
      const items = await request('/notifications');
      if (current === revision) render(items);
    } catch (error) {
      if (current === revision) status.textContent = error.message;
    } finally {
      if (current === revision) { loading = false; refreshButton.disabled = false; }
    }
  }
  refreshButton.addEventListener('click', refresh);
  loginButton.addEventListener('click', openLogin);
  window.addEventListener('focus', refresh);
  panel.addEventListener('toggle', () => { if (panel.open) refresh(); });
  return {
    setIdentity(value) {
      revision++;
      signedIn = value;
      loading = false;
      changing = false;
      displayedItems = [];
      list.replaceChildren();
      count.hidden = true;
      refreshButton.hidden = !value;
      refreshButton.disabled = false;
      loginButton.hidden = value;
      status.textContent = value ? '正在加载消息……' : '登录后查看工单消息';
      if (value) refresh();
    },
  };
}
