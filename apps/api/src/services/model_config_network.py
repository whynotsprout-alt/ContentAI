from __future__ import annotations

import ipaddress
import json
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from time import monotonic
from urllib.parse import SplitResult, unquote, urlsplit, urlunsplit

import httpx

MAX_PROBE_MODELS = 200
MAX_MODEL_ID_LENGTH = 256
MAX_PROBE_RESPONSE_BYTES = 256 * 1024
MAX_PROBE_LATENCY_MS = 60_000

Resolver = Callable[[str, int], Sequence[str]]

_IPV4_ENTERPRISE_NETWORKS = tuple(
    ipaddress.ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_IPV6_ENTERPRISE_NETWORK = ipaddress.ip_network("fc00::/7")


class ModelProbeError(RuntimeError):
    code = "MODEL_PROBE_FAILED"


class ModelEndpointForbidden(ModelProbeError):
    code = "MODEL_ENDPOINT_FORBIDDEN"
    status_code = 422


class ModelAuthenticationFailed(ModelProbeError):
    code = "MODEL_AUTH_FAILED"
    status_code = 422


class ModelNotFound(ModelProbeError):
    code = "MODEL_NOT_FOUND"
    status_code = 422


class ModelProviderUnreachable(ModelProbeError):
    code = "MODEL_PROVIDER_UNREACHABLE"
    status_code = 502


class ModelProbeFailed(ModelProbeError):
    code = "MODEL_PROBE_FAILED"
    status_code = 502


@dataclass(frozen=True)
class ModelProbeResult:
    base_url: str
    models: tuple[str, ...]
    models_truncated: bool
    model_validated: bool
    latency_ms: int


def resolve_host_addresses(host: str, port: int) -> Sequence[str]:
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        answers = None
    if answers is None:
        raise ModelProviderUnreachable("The model provider could not be reached.")
    addresses = tuple(dict.fromkeys(str(answer[4][0]) for answer in answers))
    if not addresses:
        raise ModelProviderUnreachable("The model provider could not be reached.")
    return addresses


def normalize_model_base_url(raw_url: str, resolver: Resolver = resolve_host_addresses) -> str:
    value = raw_url.strip()
    if not value or any(character.isspace() for character in value) or "\\" in value:
        raise ModelEndpointForbidden("The model endpoint is not permitted.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        parsed = None
        port = None
    if parsed is None:
        raise ModelEndpointForbidden("The model endpoint is not permitted.")
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
        or port == 0
    ):
        raise ModelEndpointForbidden("The model endpoint is not permitted.")

    scheme = parsed.scheme.lower()
    port = port or (443 if scheme == "https" else 80)
    host = parsed.hostname.lower().rstrip(".")
    if not host:
        raise ModelEndpointForbidden("The model endpoint is not permitted.")
    _reject_dot_segments(parsed.path)
    addresses = _resolve_and_validate(host, port, resolver)
    if scheme == "http" and not all(_is_enterprise_local(address) for address in addresses):
        raise ModelEndpointForbidden("Public model endpoints must use HTTPS.")

    normalized_path = parsed.path.rstrip("/")
    host_text = f"[{host}]" if ":" in host else host
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    netloc = host_text if default_port else f"{host_text}:{port}"
    return urlunsplit(SplitResult(scheme, netloc, normalized_path, "", ""))


def _reject_dot_segments(path: str) -> None:
    decoded_path = unquote(path)
    if any(segment in {".", ".."} for segment in decoded_path.split("/")):
        raise ModelEndpointForbidden("The model endpoint is not permitted.")


def _resolve_and_validate(
    host: str, port: int, resolver: Resolver
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    try:
        raw_addresses = tuple(resolver(host, port))
    except ModelProbeError:
        raise
    except (OSError, UnicodeError):
        raw_addresses = ()
    if not raw_addresses:
        raise ModelProviderUnreachable("The model provider could not be reached.")
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for raw_address in raw_addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError:
            address = None
        if address is None:
            raise ModelEndpointForbidden("The model endpoint is not permitted.")
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if (
            address.is_multicast
            or address.is_unspecified
            or address.is_reserved
            or address.is_link_local
            or not (address.is_global or _is_enterprise_local(address))
        ):
            raise ModelEndpointForbidden("The model endpoint is not permitted.")
        addresses.append(address)
    return tuple(addresses)


def _is_enterprise_local(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if address.is_loopback:
        return True
    if isinstance(address, ipaddress.IPv4Address):
        return any(address in network for network in _IPV4_ENTERPRISE_NETWORKS)
    return address in _IPV6_ENTERPRISE_NETWORK


class OpenAICompatibleProbe:
    def __init__(
        self,
        *,
        resolver: Resolver = resolve_host_addresses,
        transport: httpx.BaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self._resolver = resolver
        self._transport = transport
        self._timeout = timeout or httpx.Timeout(5.0, connect=3.0)

    def probe(self, base_url: str, api_key: str, model_name: str | None = None) -> ModelProbeResult:
        started_at = monotonic()
        normalized_url = normalize_model_base_url(base_url, self._resolver)
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        unreachable = False
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                models_payload = self._request_json(
                    client,
                    "GET",
                    f"{normalized_url}/models",
                    headers=headers,
                    base_url=normalized_url,
                    model_request=False,
                )
                models, truncated = _parse_models(models_payload)
                model_validated = False
                if model_name:
                    completion_payload = self._request_json(
                        client,
                        "POST",
                        f"{normalized_url}/chat/completions",
                        headers=headers,
                        json_payload={
                            "model": model_name,
                            "messages": [{"role": "user", "content": "ping"}],
                            "stream": False,
                            "max_tokens": 1,
                        },
                        base_url=normalized_url,
                        model_request=True,
                    )
                    _validate_completion(completion_payload)
                    model_validated = True
        except (ModelProbeError, ModelEndpointForbidden):
            raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError):
            unreachable = True
        except httpx.HTTPError:
            unreachable = True
        if unreachable:
            raise ModelProviderUnreachable("The model provider could not be reached.")

        latency_ms = min(MAX_PROBE_LATENCY_MS, max(0, round((monotonic() - started_at) * 1000)))
        return ModelProbeResult(
            base_url=normalized_url,
            models=models,
            models_truncated=truncated,
            model_validated=model_validated,
            latency_ms=latency_ms,
        )

    def _request_json(
        self,
        client: httpx.Client,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        base_url: str,
        model_request: bool,
        json_payload: dict[str, object] | None = None,
    ) -> object:
        self._revalidate(base_url)
        unreachable = False
        try:
            with client.stream(method, url, headers=headers, json=json_payload) as response:
                status_code = response.status_code
                body = _read_bounded_body(response)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError):
            unreachable = True
        if unreachable:
            raise ModelProviderUnreachable("The model provider could not be reached.")
        if 300 <= status_code < 400:
            raise ModelEndpointForbidden("Model provider redirects are not permitted.")
        if status_code in {401, 403}:
            raise ModelAuthenticationFailed("The model provider rejected the credentials.")
        if model_request and status_code == 404:
            raise ModelNotFound("The requested model was not found.")
        if status_code < 200 or status_code >= 300:
            raise ModelProbeFailed("The model provider probe failed.")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if payload is None:
            raise ModelProbeFailed("The model provider returned an invalid response.")
        return payload

    def _revalidate(self, base_url: str) -> None:
        parsed = urlsplit(base_url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = _resolve_and_validate(parsed.hostname or "", port, self._resolver)
        if parsed.scheme == "http" and not all(
            _is_enterprise_local(address) for address in addresses
        ):
            raise ModelEndpointForbidden("Public model endpoints must use HTTPS.")


def _read_bounded_body(response: httpx.Response) -> bytes:
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > MAX_PROBE_RESPONSE_BYTES:
            raise ModelProbeFailed("The model provider response exceeded the allowed size.")
    return bytes(body)


def _parse_models(payload: object) -> tuple[tuple[str, ...], bool]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ModelProbeFailed("The model provider returned an invalid response.")
    model_ids = {
        model_id
        for item in payload["data"]
        if isinstance(item, dict)
        and isinstance((model_id := item.get("id")), str)
        and model_id
        and len(model_id) <= MAX_MODEL_ID_LENGTH
    }
    ordered = sorted(model_ids)
    return tuple(ordered[:MAX_PROBE_MODELS]), len(ordered) > MAX_PROBE_MODELS


def _validate_completion(payload: object) -> None:
    if not isinstance(payload, dict):
        raise ModelProbeFailed("The model provider returned an invalid response.")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ModelProbeFailed("The model provider returned an invalid response.")
