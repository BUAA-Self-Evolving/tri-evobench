// One private adapter-owned receipt. Native hooks do not own a second counter.
const { readFileSync, writeFileSync, renameSync } = require('node:fs');
const path = process.env.TRIEVO_NATIVE_BUDGET;
if (path) {
  const outputCap = JSON.parse(readFileSync(path, 'utf8')).limits.max_output_tokens;
  Object.defineProperty(globalThis, '__trievoBudget', { value: Object.freeze({
    outputCap() { return outputCap; },
    admit(kind) {
      const value = JSON.parse(readFileSync(path, 'utf8'));
      const counter = kind + '_calls';
      const reason = Date.now() >= value.deadline_ms ? 'episode_timeout_seconds'
        : value[counter] >= value.limits['max_' + counter] ? 'max_' + counter : null;
      if (reason) {
        if (!value.limit_reasons.includes(reason)) value.limit_reasons.push(reason);
      } else value[counter]++;
      writeFileSync(path + '.tmp', JSON.stringify(value));
      renameSync(path + '.tmp', path);
      if (reason) throw new Error('TRIEVO_LIMIT_' + reason);
    },
  }), writable: false, configurable: false });
}
