// SDK transport compatibility only: the pinned SDK creates unknown identities
// but does not open existing durable sessions. Use the native resume API before
// any prompt is submitted. No model/tool/reward logic is replaced.
import { HarnessSdkJsonRpcServer } from '/opt/deepseek-harness/packages/sdk/server/lib/index.js';

export const name = 'trievo-sdk-durable-session';
export function apply(ctx) {
  const prototype = HarnessSdkJsonRpcServer.prototype;
  const original = prototype.createSession;
  prototype.createSession = async function (sessionId) {
    try {
      return await original.call(this, sessionId);
    } catch (error) {
      if (error?.name !== 'SessionAlreadyExistsError' || error.sessionId !== sessionId) throw error;
      const handle = await this.ctx.agents.resume({
        resumeSessionId: sessionId,
        agentOptions: {
          provider: this.provider,
          model: this.model,
          ...(this.reasoningEffort === undefined ? {} : { reasoningEffort: this.reasoningEffort }),
          ...(this.maxTokens === undefined ? {} : { maxTokens: this.maxTokens }),
        },
      });
      const record = { handle, failure: undefined };
      this.sessions.set(sessionId, record);
      return record;
    }
  };
  ctx.effect(() => () => { prototype.createSession = original; });
}
