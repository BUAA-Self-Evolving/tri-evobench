// Shared native hooks. Synchronous updates serialize concurrently scheduled tools.
import { readFileSync, writeFileSync, renameSync } from 'node:fs';
export function admission(path, kind) {
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
}
export function limits(path) { return JSON.parse(readFileSync(path, 'utf8')).limits; }
