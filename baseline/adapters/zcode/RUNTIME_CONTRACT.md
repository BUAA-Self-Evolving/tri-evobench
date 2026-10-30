# ZCode benchmark runtime integration

The pinned upstream archive remains unchanged on the host. Image-only, guarded
instrumentation binds its native model wrapper and public tool executor to the
same foreground admission receipt used by the other V5 adapters. This is adapter
instrumentation, not a new learner or a change to task/score semantics.

`start_turn` validates the declared Task terminal socket before acknowledging it.
Each Interaction creates one private budget receipt; model/tool admission happens
before dispatch/side effects, and output tokens are clamped to the declared cap.
The native shell uses the existing Task terminal proxy. Credentials and native
state remain outside the offline Task container. Budget errors are not retries.
Only the native CWD capture file uses a dedicated directory on the ephemeral
terminal volume, so successful `cd` changes reach the native owner. Other temp
files and provider/session state keep their private paths. The CWD helper contains
only the public working directory and is excluded from workspace/state ZIPs.

The adapter is the single owner of the receipt and Interaction deadline. The
native hooks only update its counters synchronously; the host reads the receipt
after the original process exits. A limit-exhausted terminal result retains actual
native usage and saved output. Missing usage/result stays an infrastructure error.

```text
host Interaction begin → private receipt → native model/tool admission
                                          ↓
                                  offline Task terminal
                                          ↓
native result + usage → receipt + saved reply → host verifier → Turn checkpoint
```

The original fresh-per-Turn session and disabled native learner remain explicit.
Reset/Frozen/Evolving are separate evaluations under the shared runner policy;
unsupported learning is disclosed, and scores are never copied between states.
The integration does not authorize replay of old side effects or paid requests.
