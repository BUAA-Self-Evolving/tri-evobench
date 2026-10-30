"""Inspect public model IDs/protocol availability without logging credentials."""
import json
import os
import urllib.request
import urllib.error
import argparse
import re


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="deepseek-v4-flash")
    args = parser.parse_args()
    key = os.environ["FRIDAY_APP_ID_1"]
    base = "https://aigc.sankuai.com/v1/openai/native"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for path in ("/models",):
        request = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + key})
        try:
            with opener.open(request, timeout=25) as response:
                data = json.load(response)
            names = [item.get("id") for item in data.get("data", []) if isinstance(item, dict)]
            print(json.dumps({"path": path, "model_ids": [n for n in names if n and "deepseek" in n.lower()]}, ensure_ascii=False))
            print(json.dumps({"response_fields": sorted(data.keys())}))
            rows = data.get("data", [])
            print(json.dumps({"model_count": len(rows), "first_model_fields": sorted(rows[0].keys()) if rows and isinstance(rows[0], dict) else []}))
        except urllib.error.HTTPError as exc:
            print(json.dumps({"path": path, "http_status": exc.code}))
        except Exception as exc:
            print(json.dumps({"path": path, "error_type": type(exc).__name__}))
    model = args.model
    for path in ("/chat/completions", "/messages"):
        payload = json.dumps({"model": model, "messages": [{"role": "user", "content": "Reply OK."}],
                              "max_tokens": 16, "stream": False}).encode()
        request = urllib.request.Request(base + path, data=payload, headers={
            "Authorization": "Bearer " + key, "x-api-key": key,
            "anthropic-version": "2023-06-01", "Content-Type": "application/json"})
        try:
            with opener.open(request, timeout=40) as response:
                status = response.status
                data = json.load(response)
            print(json.dumps({"path": path, "requested_model": model, "http_status": status,
                              "response_fields": sorted(data.keys()),
                              "has_choices": bool(data.get("choices")),
                              "has_message_content": bool(data.get("content")),
                              "model_matches": data.get("model") == model,
                              "returned_model": data.get("model") if isinstance(data.get("model"), str) and "deepseek" in data["model"].lower() and re.fullmatch(r"[A-Za-z0-9._/ -]{1,128}", data["model"]) and key not in data["model"] else None}))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace").lower()
            print(json.dumps({"path": path, "requested_model": model, "http_status": exc.code,
                              "error_mentions_model": "model" in body or "模型" in body,
                              "error_mentions_unsupported": any(word in body for word in ("not support", "unsupported", "not found", "不存在", "不支持")),
                              "error_mentions_auth": any(word in body for word in ("unauthorized", "appid", "api key", "认证"))}))
        except Exception as exc:
            print(json.dumps({"path": path, "error_type": type(exc).__name__}))


if __name__ == "__main__":
    main()
