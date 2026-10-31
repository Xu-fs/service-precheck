/* POST流的SSE解析器：数据块与事件边界无关，逐行处理CR/LF及中文多字节。 */
export async function readSSE(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let line = '', event = '', data = [], skipLF = false, terminal = false;
  async function finishLine() {
    if (line === '') {
      if (data.length) {
        const payload = JSON.parse(data.join('\n'));
        terminal = Boolean(await onEvent(event || 'message', payload));
      }
      event = ''; data = [];
    } else if (!line.startsWith(':')) {
      const colon = line.indexOf(':');
      const field = colon < 0 ? line : line.slice(0, colon);
      let value = colon < 0 ? '' : line.slice(colon + 1);
      if (value.startsWith(' ')) value = value.slice(1);
      if (field === 'event') event = value;
      if (field === 'data') data.push(value);
    }
    line = '';
  }
  async function consume(text) {
    for (const character of text) {
      if (terminal) break;
      if (character === '\r') { await finishLine(); skipLF = true; }
      else if (character === '\n') { if (!skipLF) await finishLine(); skipLF = false; }
      else { skipLF = false; line += character; }
    }
  }
  try {
    while (!terminal) {
      const { value, done } = await reader.read();
      if (done) { await consume(decoder.decode()); break; }
      await consume(decoder.decode(value, { stream: true }));
    }
    if (!terminal) throw new Error('连接已中断，正在核对消息状态。');
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
