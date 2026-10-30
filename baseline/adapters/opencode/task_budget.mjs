import { admission, limits } from '/opt/trievo/baseline/adapters/native_budget.mjs';
export default async function () {
  const path = process.env.TRIEVO_NATIVE_BUDGET;
  if (!path) throw new Error('V5 native budget file missing');
  return {
    'chat.params': async (_input, output) => {
      admission(path, 'model');
      output.maxOutputTokens = Math.min(output.maxOutputTokens ?? Infinity, limits(path).max_output_tokens);
    },
    'tool.execute.before': async () => { admission(path, 'tool'); },
  };
}
