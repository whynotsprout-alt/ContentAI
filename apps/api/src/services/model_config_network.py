from __future__ import annotations

import asyncio
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
DEFAULT_ASYNC_RESOLVER_TIMEOUT_SECONDS = 3.0

Resolver = Callable[[str, int], Sequence[str]]

_IPV4_ENTERPRISE_NETWORKS = tuple(
    ipaddress.ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_IPV6_ENTERPRISE_NETWORK = ipaddress.ip_network("fc00::/7")
_NAT64_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)
_METADATA_ADDRESSES = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("168.63.129.16"),
    ipaddress.ip_address("fd00:ec2::254"),
}


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


def _model_origin(parsed: SplitResult) -> tuple[str, str, int]:
    scheme = parsed.scheme.lower()
    host = _normalize_hostname(parsed.hostname or "")
    if scheme not in {"http", "https"} or not host:
        raise ModelEndpointForbidden("The model endpoint is not permitted.")
    try:
        port = parsed.port
    except ValueError:
        raise ModelEndpointForbidden("The model endpoint is not permitted.") from None
    return scheme, host, port or (443 if scheme == "https" else 80)


def _pinned_request_target(
    url: str,
    base_url: str,
    resolver: Resolver,
) -> tuple[str, str, str | None]:
    configured = urlsplit(base_url)
    request_url = urlsplit(url)
    configured_origin = _model_origin(configured)
    if (
        request_url.username is not None
        or request_url.password is not None
        or _model_origin(request_url) != configured_origin
    ):
        raise ModelEndpointForbidden("The model endpoint is not permitted.")

    scheme, host, port = configured_origin
    addresses = _resolve_and_validate(host, port, resolver)
    if scheme == "http" and not all(_is_enterprise_local(address) for address in addresses):
        raise ModelEndpointForbidden("Public model endpoints must use HTTPS.")

    selected_address = addresses[0]
    selected_host = (
        f"[{selected_address}]" if selected_address.version == 6 else str(selected_address)
    )
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    pinned_netloc = selected_host if default_port else f"{selected_host}:{port}"
    pinned_url = urlunsplit(
        SplitResult(
            request_url.scheme,
            pinned_netloc,
            request_url.path,
            request_url.query,
            request_url.fragment,
        )
    )
    return (
        pinned_url,
        configured.netloc,
        host if scheme == "https" else None,
    )


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
    host = _normalize_hostname(parsed.hostname)
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


def _normalize_hostname(hostname: str) -> str:
    host = hostname.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        return str(address)
    try:
        idna_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        idna_host = None
    if idna_host is None:
        raise ModelEndpointForbidden("The model endpoint is not permitted.")
    return idna_host


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
        if not _is_allowed_address(address):
            raise ModelEndpointForbidden("The model endpoint is not permitted.")
        if isinstance(address, ipaddress.IPv6Address):
            for embedded_address in _embedded_ipv4_addresses(address):
                if not _is_allowed_address(embedded_address):
                    raise ModelEndpointForbidden("The model endpoint is not permitted.")
        addresses.append(address)
    return tuple(addresses)


def _is_allowed_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (
        address in _METADATA_ADDRESSES
        or address.is_multicast
        or address.is_unspecified
        or address.is_link_local
        or (isinstance(address, ipaddress.IPv6Address) and address.is_site_local)
    ):
        return False
    if _is_enterprise_local(address):
        return True
    return address.is_global and not address.is_reserved


def _embedded_ipv4_addresses(address: ipaddress.IPv6Address) -> tuple[ipaddress.IPv4Address, ...]:
    embedded: list[ipaddress.IPv4Address] = []
    if address.sixtofour is not None:
        embedded.append(address.sixtofour)
    if address.teredo is not None:
        server, client = address.teredo
        embedded.extend((server, client))
    if any(address in network for network in _NAT64_NETWORKS):
        embedded.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return tuple(dict.fromkeys(embedded))


def _is_enterprise_local(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if address.is_loopback:
        return True
    if isinstance(address, ipaddress.IPv4Address):
        return any(address in network for network in _IPV4_ENTERPRISE_NETWORKS)
    return address in _IPV6_ENTERPRISE_NETWORK


class PinnedModelTransport(httpx.BaseTransport):
    def __init__(
        self,
        *,
        base_url: str,
        resolver: Resolver = resolve_host_addresses,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._resolver = resolver
        self._transport = transport or httpx.HTTPTransport(trust_env=False, retries=0)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        pinned_url, host_header, sni_hostname = _pinned_request_target(
            str(request.url), self._base_url, self._resolver
        )
        headers = request.headers.copy()
        headers["Host"] = host_header
        extensions = dict(request.extensions)
        if sni_hostname is not None:
            extensions["sni_hostname"] = sni_hostname
        pinned_request = httpx.Request(
            request.method,
            pinned_url,
            headers=headers,
            stream=request.stream,
            extensions=extensions,
        )
        return self._transport.handle_request(pinned_request)

    def close(self) -> None:
        self._transport.close()


class PinnedAsyncModelTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        *,
        base_url: str,
        resolver: Resolver = resolve_host_addresses,
        resolver_timeout_seconds: float = DEFAULT_ASYNC_RESOLVER_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._resolver = resolver
        self._resolver_timeout_seconds = max(0.01, float(resolver_timeout_seconds))
        self._transport = transport or httpx.AsyncHTTPTransport(
            trust_env=False,
            retries=0,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        resolution_timed_out = False
        try:
            pinned_url, host_header, sni_hostname = await asyncio.wait_for(
                asyncio.to_thread(
                    _pinned_request_target,
                    str(request.url),
                    self._base_url,
                    self._resolver,
                ),
                timeout=self._resolver_timeout_seconds,
            )
        except TimeoutError:
            resolution_timed_out = True
        if resolution_timed_out:
            raise ModelProviderUnreachable("The model provider could not be reached.")
        headers = request.headers.copy()
        headers["Host"] = host_header
        extensions = dict(request.extensions)
        if sni_hostname is not None:
            extensions["sni_hostname"] = sni_hostname
        pinned_request = httpx.Request(
            request.method,
            pinned_url,
            headers=headers,
            stream=request.stream,
            extensions=extensions,
        )
        return await self._transport.handle_async_request(pinned_request)

    async def aclose(self) -> None:
        await self._transport.aclose()


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
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        models_payload = self._request_json(
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
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        base_url: str,
        model_request: bool,
        json_payload: dict[str, object] | None = None,
    ) -> object:
        pinned_url, host_header, sni_hostname = _pinned_request_target(
            url,
            base_url,
            self._resolver,
        )
        request_headers = {**headers, "Host": host_header}
        extensions = {"sni_hostname": sni_hostname} if sni_hostname is not None else None
        unreachable = False
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            ) as client:
                with client.stream(
                    method,
                    pinned_url,
                    headers=request_headers,
                    json=json_payload,
                    extensions=extensions,
                ) as response:
                    status_code = response.status_code
                    if 300 <= status_code < 400:
                        raise ModelEndpointForbidden(
                            "Model provider redirects are not permitted."
                        )
                    if status_code in {401, 403}:
                        raise ModelAuthenticationFailed(
                            "The model provider rejected the credentials."
                        )
                    if model_request and status_code == 404:
                        raise ModelNotFound("The requested model was not found.")
                    if status_code < 200 or status_code >= 300:
                        raise ModelProbeFailed("The model provider probe failed.")
                    content_encoding = response.headers.get("Content-Encoding", "").strip()
                    if content_encoding and content_encoding.lower() != "identity":
                        raise ModelProbeFailed(
                            "The model provider returned an unsupported response encoding."
                        )
                    body = _read_bounded_body(response)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError, httpx.HTTPError):
            unreachable = True
        if unreachable:
            raise ModelProviderUnreachable("The model provider could not be reached.")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if payload is None:
            raise ModelProbeFailed("The model provider returned an invalid response.")
        return payload

def _read_bounded_body(response: httpx.Response) -> bytes:
    if response.is_stream_consumed:
        if len(response.content) > MAX_PROBE_RESPONSE_BYTES:
            raise ModelProbeFailed("The model provider response exceeded the allowed size.")
        return response.content
    body = bytearray()
    for chunk in response.iter_raw():
        if len(chunk) > MAX_PROBE_RESPONSE_BYTES - len(body):
            raise ModelProbeFailed("The model provider response exceeded the allowed size.")
        body.extend(chunk)
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
    message = choices[0].get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ModelProbeFailed("The model provider returned an invalid response.")
