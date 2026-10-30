"""Run the shared, loopback-only Friday request gateway (no upstream retries)."""
import argparse
import json
import os
from pathlib import Path
import sys
import threading

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from trievo_bench.friday_gateway import DEFAULT_POLICY, FridayGateway, GATEWAY_SCHEMA
from trievo_bench.provider_profiles import FRIDAY_MODELS, provider_credentials


def load_pool(app_id_file=None):
    environment = dict(os.environ)
    if app_id_file:
        values = [line.strip() for line in Path(app_id_file).read_text().splitlines() if line.strip()]
        environment = {key: value for key, value in environment.items() if not key.startswith("FRIDAY_APP_ID")}
        environment.update({f"FRIDAY_APP_ID_{index}": value for index, value in enumerate(values, 1)})
    return provider_credentials("friday", environment)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-id-file", type=Path)
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--model", choices=FRIDAY_MODELS, default=FRIDAY_MODELS[0])
    parser.add_argument("--state", type=Path, default=REPO / ".tmp/friday-shared-budget.sqlite")
    parser.add_argument("--policy-json", help="credential-free explicit gateway policy, including optional slot_overrides")
    args = parser.parse_args()
    policy = json.loads(args.policy_json) if args.policy_json else None
    gateway = FridayGateway(load_pool(args.app_id_file), args.state, port=args.port, model=args.model, policy=policy)
    # Health endpoints are local-only and never invoke the model.
    thread = threading.Thread(target=gateway.server.serve_forever, daemon=True)
    thread.start()
    print(json.dumps({"schema": GATEWAY_SCHEMA, "base_url": gateway.base_url,
                      "policy": gateway.policy, "slots": len(gateway.credentials)}), flush=True)
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        gateway.close()


if __name__ == "__main__":
    main()
