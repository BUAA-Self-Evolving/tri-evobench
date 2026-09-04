"""Short-lived proxy bridge for Docker adapters behind a loopback-only proxy.

Some managed development environments expose Docker through a nested/rootless
daemon while the operator's HTTP proxy listens only on the development
container's loopback interface.  A child Docker container cannot use that
``127.0.0.1`` endpoint.  When explicitly enabled, this module follows the same
boundary used by the SETA runtime: bind a scoped TCP forwarder to the
development container's default-route address and pass that reachable URL to
the Docker adapter.

The bridge is transport only.  It does not change task network policy, proxy
credentials, benchmark data, or verifier isolation.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit


class DockerProxyBridgeError(RuntimeError):
    pass


_PROXY_PAIRS = (
    ("HTTP_PROXY", "http_proxy"),
    ("HTTPS_PROXY", "https_proxy"),
)
_FALSE_VALUES = {"", "0", "false", "no", "off", "disabled"}
_TRUE_VALUES = {"1", "true", "yes", "on", "auto", "required"}


@dataclass
class DockerProxyBridge:
    """One flow-scoped set of TCP forwarders and its container environment."""

    environment: dict[str, str] = field(default_factory=dict)
    _processes: list[subprocess.Popen[bytes]] = field(default_factory=list, repr=False)

    @property
    def active(self) -> bool:
        return bool(self._processes)

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "DockerProxyBridge":
        source = dict(os.environ if environ is None else environ)
        mode = source.get("TRIEVO_DOCKER_PROXY_BRIDGE", "off").strip().lower()
        if mode in _FALSE_VALUES:
            return cls()
        if mode not in _TRUE_VALUES:
            raise DockerProxyBridgeError(
                "TRIEVO_DOCKER_PROXY_BRIDGE must be off, auto, or required"
            )

        configured = _configured_proxies(source)
        if not configured:
            if mode == "required":
                raise DockerProxyBridgeError(
                    "Docker proxy bridge is required but HTTP_PROXY/HTTPS_PROXY is absent"
                )
            return cls()

        endpoints = {
            raw_proxy: _loopback_proxy_endpoint(raw_proxy)
            for _, _, raw_proxy in configured
        }
        has_loopback_endpoint = any(endpoint is not None for endpoint in endpoints.values())
        listen_host = ""
        allow_range = ""
        if has_loopback_endpoint:
            listen_host = source.get("TRIEVO_DOCKER_PROXY_HOST", "").strip()
            if not listen_host:
                listen_host = _default_route_source()
            _validate_listen_host(listen_host)
            allow_range = source.get("TRIEVO_DOCKER_PROXY_RANGE", "").strip()
            if not allow_range:
                raise DockerProxyBridgeError(
                    "TRIEVO_DOCKER_PROXY_RANGE is required for a loopback proxy; "
                    "set it to the observed Docker client source CIDR"
                )
            _validate_allow_range(allow_range)

        bridge = cls()
        forwarded: dict[str, str] = {}
        try:
            for upper, lower, raw_proxy in configured:
                endpoint = endpoints[raw_proxy]
                if endpoint is None:
                    effective = raw_proxy
                else:
                    effective = forwarded.get(raw_proxy, "")
                    if not effective:
                        port = _allocate_port(listen_host)
                        process = _start_socat_forwarder(
                            listen_host=listen_host,
                            listen_port=port,
                            allow_range=allow_range,
                            target_host=endpoint[0],
                            target_port=endpoint[1],
                        )
                        bridge._processes.append(process)
                        _wait_until_listening(process, listen_host, port)
                        effective = f"http://{listen_host}:{port}"
                        forwarded[raw_proxy] = effective
                bridge.environment[upper] = effective
                bridge.environment[lower] = effective

            no_proxy = _first_consistent_value(source, "NO_PROXY", "no_proxy")
            bridge.environment["NO_PROXY"] = _append_no_proxy(no_proxy, listen_host)
            bridge.environment["no_proxy"] = bridge.environment["NO_PROXY"]
            return bridge
        except BaseException:
            bridge.close()
            raise

    def close(self) -> None:
        for process in reversed(self._processes):
            if process.poll() is not None:
                continue
            process.terminate()
        for process in reversed(self._processes):
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        self._processes.clear()


def is_docker_command(command: list[str]) -> bool:
    if not command:
        return False
    executable = os.path.basename(command[0]).lower()
    return executable in {"docker", "docker.exe"}


def _configured_proxies(source: Mapping[str, str]) -> list[tuple[str, str, str]]:
    configured: list[tuple[str, str, str]] = []
    for upper, lower in _PROXY_PAIRS:
        value = _first_consistent_value(source, upper, lower)
        if value:
            configured.append((upper, lower, value))
    return configured


def _first_consistent_value(
    source: Mapping[str, str],
    upper: str,
    lower: str,
) -> str:
    upper_value = source.get(upper, "").strip()
    lower_value = source.get(lower, "").strip()
    if upper_value and lower_value and upper_value != lower_value:
        raise DockerProxyBridgeError(
            f"conflicting {upper}/{lower} values; normalize the host proxy first"
        )
    return upper_value or lower_value


def _loopback_proxy_endpoint(raw_proxy: str) -> tuple[str, int] | None:
    parsed = urlsplit(raw_proxy)
    if parsed.username is not None or parsed.password is not None:
        raise DockerProxyBridgeError(
            "authenticated proxy URLs are not supported by the local Docker bridge"
        )
    if parsed.scheme != "http" or parsed.hostname is None or parsed.port is None:
        raise DockerProxyBridgeError(
            "Docker proxy bridge requires an explicit http://host:port proxy URL"
        )
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise DockerProxyBridgeError("proxy URL must not contain a path, query, or fragment")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.lower() == "localhost"
    if not loopback:
        return None
    return ("127.0.0.1" if parsed.hostname.lower() == "localhost" else parsed.hostname, parsed.port)


def _default_route_source() -> str:
    try:
        completed = subprocess.run(
            ["ip", "-4", "route", "get", "1.1.1.1"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise DockerProxyBridgeError(
            "cannot discover a Docker-reachable host address; set TRIEVO_DOCKER_PROXY_HOST"
        ) from exc
    fields = completed.stdout.split()
    for index, field in enumerate(fields[:-1]):
        if field == "src":
            return fields[index + 1]
    raise DockerProxyBridgeError(
        "default route has no source address; set TRIEVO_DOCKER_PROXY_HOST"
    )


def _validate_listen_host(value: str) -> None:
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise DockerProxyBridgeError(
            "TRIEVO_DOCKER_PROXY_HOST must be an explicit IPv4 address"
        ) from exc
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        raise DockerProxyBridgeError(
            "TRIEVO_DOCKER_PROXY_HOST must be a non-loopback unicast address"
        )


def _validate_allow_range(value: str) -> None:
    try:
        ipaddress.IPv4Network(value, strict=False)
    except (ipaddress.AddressValueError, ValueError) as exc:
        raise DockerProxyBridgeError(
            "TRIEVO_DOCKER_PROXY_RANGE must be an IPv4 CIDR"
        ) from exc


def _allocate_port(listen_host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((listen_host, 0))
        return int(probe.getsockname()[1])


def _start_socat_forwarder(
    *,
    listen_host: str,
    listen_port: int,
    allow_range: str,
    target_host: str,
    target_port: int,
) -> subprocess.Popen[bytes]:
    socat = shutil.which("socat")
    if socat is None:
        raise DockerProxyBridgeError(
            "socat is required when TRIEVO_DOCKER_PROXY_BRIDGE is enabled"
        )
    return subprocess.Popen(
        [
            socat,
            (
                f"TCP4-LISTEN:{listen_port},bind={listen_host},reuseaddr,fork,"
                f"range={allow_range}"
            ),
            f"TCP4:{target_host}:{target_port}",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_until_listening(
    process: subprocess.Popen[bytes],
    listen_host: str,
    listen_port: int,
) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise DockerProxyBridgeError(
                f"Docker proxy bridge exited during startup with code {code}"
            )
        try:
            with socket.create_connection((listen_host, listen_port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise DockerProxyBridgeError("Docker proxy bridge did not become reachable")


def _append_no_proxy(current: str, listen_host: str) -> str:
    entries = [item.strip() for item in current.split(",") if item.strip()]
    for item in ("localhost", "127.0.0.1", listen_host):
        if item and item not in entries:
            entries.append(item)
    return ",".join(entries)
