/** POST + 流式响应解析（2026-10-20）。
 *
 * 浏览器的 EventSource 只能发 GET，而生成请求要带文档上下文和消息体，
 * 所以这里用 fetch + ReadableStream 自己拆 SSE。
 *
 * 两个必须处理的细节：
 *  1. 一次 `read()` 可能带回多个事件，也可能只带半个事件 —— 按 `\n\n` 分包并保留尾巴；
 *  2. 中文是多字节 UTF-8，网络分片可能切在字中间 —— `TextDecoder({stream:true})`
 *     负责把半个字留到下一次解码，否则中文会变成乱码。
 */
export type SSEEvent = { event: string; data: string };

export async function readSSE(
  path: string,
  body: unknown,
  handlers: { onEvent: (ev: SSEEvent) => void; signal?: AbortSignal },
): Promise<void> {
  const res = await fetch(`/api${path}`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json", accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal: handlers.signal,
  });
  if (!res.ok) {
    const raw = await res.text();
    let msg = raw;
    try {
      const j = JSON.parse(raw) as Record<string, unknown>;
      const d = j.detail ?? j.message ?? j.error;
      if (typeof d === "string") msg = d;
    } catch {
      /* 非 JSON：保留原文，下面统一裁剪 */
    }
    throw new Error((msg || `请求失败（${res.status}）`).slice(0, 300));
  }
  if (!res.body) throw new Error("当前浏览器不支持流式响应。");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";

  const drain = () => {
    for (;;) {
      const m = /\r?\n\r?\n/.exec(buf);
      if (!m) return;
      const raw = buf.slice(0, m.index);
      buf = buf.slice(m.index + m[0].length);
      const ev = parseEvent(raw);
      if (ev) handlers.onEvent(ev);
    }
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    drain();
  }
  buf += decoder.decode();
  drain();
  const tail = parseEvent(buf);
  if (tail) handlers.onEvent(tail);
}

function parseEvent(raw: string): SSEEvent | null {
  let event = "message";
  const data: string[] = [];
  for (const line of raw.split(/\r?\n/)) {
    if (!line || line.startsWith(":")) continue;
    const i = line.indexOf(":");
    const field = i < 0 ? line : line.slice(0, i);
    let value = i < 0 ? "" : line.slice(i + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  if (!data.length) return null;
  return { event, data: data.join("\n") };
}
