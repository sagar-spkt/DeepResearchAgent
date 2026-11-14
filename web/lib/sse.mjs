export function eventParser(onEvent) {
  let buffer = '';
  return chunk => {
    buffer += chunk;
    buffer = buffer.replace(/\r\n/g, '\n');
    let boundary;
    while ((boundary = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2);
      let id; let event = 'message'; const data = [];
      for (const line of frame.split('\n')) {
        if (line.startsWith('id:')) id = line.slice(3).trim();
        else if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) data.push(line.slice(5).trimStart());
      }
      if (data.length) {
        let value;
        try { value = JSON.parse(data.join('\n')); } catch { value = {message: data.join('\n')}; }
        onEvent({id, event, data: value});
      }
    }
  };
}
export async function streamRun(id, onEvent, {signal, after} = {}) {
  const response = await fetch(`/api/runs/${encodeURIComponent(id)}/events${after ? `?after=${encodeURIComponent(after)}` : ''}`, {credentials: 'same-origin', signal});
  if (!response.ok || !response.body) throw new Error(`Unable to stream research (${response.status})`);
  const reader = response.body.getReader();
  const decoder = new TextDecoder(); const parse = eventParser(onEvent);
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      parse(decoder.decode(value, {stream: true}));
    }
    parse(decoder.decode());
  } finally { reader.releaseLock(); }
}
