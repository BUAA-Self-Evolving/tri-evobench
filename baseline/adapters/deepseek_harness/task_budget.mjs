import { admission, limits } from '/opt/trievo/baseline/adapters/native_budget.mjs';
export const name = 'trievo-v5-native-budget';
export const inject = ['tools', 'llm'];
export function apply(ctx) {
  const path = process.env.TRIEVO_NATIVE_BUDGET;
  if (!path) throw new Error('V5 native budget file missing');
  ctx.on('llm/stream', async function* (_options, next) {
    admission(path, 'model');
    yield* next();
  });
  ctx.tools.guard(exec => {
    try { admission(path, 'tool'); }
    catch (error) {
      exec.agent?.cancel({ kind: 'user' });
      return error.message;
    }
  });
  ctx.on('tools/execute', async (exec, next) => {
    const previous = exec.signal;
    exec.signal = AbortSignal.any([previous,
      AbortSignal.timeout(Math.ceil(limits(path).terminal_timeout_seconds * 1000))]);
    try { return await next(); } finally { exec.signal = previous; }
  });
}
