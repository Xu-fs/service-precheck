// Node内置测试：真实解析器必须支持任意字节分包、多个data行与截断检测。
import test from 'node:test';
import assert from 'node:assert/strict';
import { readSSE } from '../src/zhengwu_precheck/static/js/sse.js';

function response(text, size) {
  const bytes = new TextEncoder().encode(text);
  return new Response(new ReadableStream({start(controller) {
    for (let i = 0; i < bytes.length; i += size) controller.enqueue(bytes.slice(i, i + size));
    controller.close();
  }}));
}

for (const size of [1, 2, 7, 999]) test(`中文SSE每块${size}字节`, async () => {
  const seen = [];
  const text = ': ping\r\nevent: answer_delta\r\ndata: {"text":"中文材料<img>"}\r\n\r\nevent: done\ndata: {"persisted":true}\n\n';
  await readSSE(response(text, size), (event, data) => { seen.push([event, data]); return event === 'done'; });
  assert.deepEqual(seen, [['answer_delta', {text: '中文材料<img>'}], ['done', {persisted: true}]]);
});
test('多data行与CR换行', async () => {
  const seen = [];
  await readSSE(response('event: done\rdata: {\rdata: "persisted":true}\r\r', 1), (event, data) => { seen.push(data); return true; });
  assert.deepEqual(seen, [{persisted: true}]);
});
test('缺少终态不能标记完成', async () => {
  await assert.rejects(readSSE(response('event: answer_delta\ndata: {"text":"半条"}\n\n', 2), () => false), /连接已中断/);
});
