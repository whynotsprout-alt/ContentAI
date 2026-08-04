from __future__ import annotations

import asyncio
import gzip
import json
import threading
import time
from collections.abc import Sequence
from importlib import import_module

import httpx
import pytest


def _network_module():
    try:
        return import_module("contentai.services.model_config_network")
    except ModuleNotFoundError:
        pytest.fail("model configuration network safety module is missing")


class Resolver:
    def __init__(self, *answers: Sequence[str]) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> Sequence[str]:
        self.calls.append((host, port))
        if not self.answers:
            raise AssertionError("unexpected DNS resolution")
        return self.answers.pop(0)


class ClosingMockTransport(httpx.MockTransport):
    def __init__(self, handler) -> None:
        super().__init__(handler)
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        super().close()


class TrackingStream(httpx.SyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        yield self.content


@pytest.mark.parametrize(
    "url",
    [
        "ftp://api.example.test/v1",
        "https://user:password@api.example.test/v1",
        "https://api.example.test/v1?token=value",
        "https://api.example.test/v1#fragment",
        "https://api.example.test/v1?",
        "https://api.example.test/v1#",
        "https://api.example.test:0/v1",
        "https://api.example.test/v1/%2e%2e/internal",
    ],
)
def test_model_base_url_rejects_unsafe_url_components(url: str) -> None:
    network = _network_module()

    with pytest.raises(network.ModelEndpointForbidden):
        network.normalize_model_base_url(url, Resolver(["93.184.216.34"]))


def test_model_base_url_keeps_openai_api_root_exact() -> None:
    network = _network_module()

    normalized = network.normalize_model_base_url(
        "https://API.EXAMPLE.TEST/v1/", Resolver(["93.184.216.34"])
    )

    assert normalized == "https://api.example.test/v1"
    assert "/openai" not in normalized
    assert "/anthropic" not in normalized


def test_invalid_idna_endpoint_error_has_no_exception_chain() -> None:
    network = _network_module()

    with pytest.raises(network.ModelEndpointForbidden) as exc_info:
        network.normalize_model_base_url("https://\ud800.example/v1", Resolver([]))

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0",
        "169.254.169.254",
        "224.0.0.1",
        "240.0.0.1",
        "100.64.0.1",
        "::",
        "fe80::1",
        "ff02::1",
        "fec0::1",
        "fd00:ec2::254",
        "168.63.129.16",
        "::ffff:169.254.169.254",
        "2002:a9fe:a9fe::1",
        "2001:0000:4136:e378:8000:63bf:5601:5601",
        "64:ff9b::a9fe:a9fe",
    ],
)
def test_model_endpoint_rejects_forbidden_address_classes(address: str) -> None:
    network = _network_module()

    with pytest.raises(network.ModelEndpointForbidden):
        network.normalize_model_base_url(
            "https://api.example.test/v1", Resolver([address])
        )


def test_public_http_is_rejected_but_private_http_is_accepted() -> None:
    network = _network_module()

    with pytest.raises(network.ModelEndpointForbidden):
        network.normalize_model_base_url(
            "http://api.example.test/v1", Resolver(["93.184.216.34"])
        )

    assert (
        network.normalize_model_base_url(
            "http://models.enterprise.test/v1", Resolver(["10.20.30.40"])
        )
        == "http://models.enterprise.test/v1"
    )


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "::1", "::ffff:10.20.30.40"],
)
def test_loopback_and_mapped_rfc1918_http_endpoints_are_accepted(address: str) -> None:
    network = _network_module()

    assert network.normalize_model_base_url(
        "http://models.enterprise.test/v1", Resolver([address])
    ) == "http://models.enterprise.test/v1"


def test_mixed_public_and_forbidden_dns_answers_are_rejected() -> None:
    network = _network_module()

    with pytest.raises(network.ModelEndpointForbidden):
        network.normalize_model_base_url(
            "https://api.example.test/v1",
            Resolver(["93.184.216.34", "169.254.169.254"]),
        )


def test_probe_revalidates_dns_before_every_request_and_uses_exact_paths() -> None:
    network = _network_module()
    resolver = Resolver(
        ["93.184.216.34"],
        ["93.184.216.34"],
        ["93.184.216.34"],
    )
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={"data": [{"id": "z-model"}, {"id": "a-model"}, {"id": "z-model"}]},
            )
        assert request.url.path == "/v1/chat/completions"
        assert json.loads(request.content) == {
            "model": "custom-model",
            "messages": [{"role": "user", "content": "ping"}],
            "stream": False,
            "max_tokens": 1,
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    transport = ClosingMockTransport(handle)
    prober = network.OpenAICompatibleProbe(
        resolver=resolver,
        transport=transport,
    )

    result = prober.probe(
        "https://api.example.test/v1", "test-secret-key", "custom-model"
    )

    assert result.models == ("a-model", "z-model")
    assert result.model_validated is True
    assert [request.url.path for request in requests] == [
        "/v1/models",
        "/v1/chat/completions",
    ]
    assert len(resolver.calls) == 3
    assert all(request.headers["Authorization"] == "Bearer test-secret-key" for request in requests)
    assert all(request.url.host == "93.184.216.34" for request in requests)
    assert all(request.headers["Host"] == "api.example.test" for request in requests)
    assert all(
        request.extensions["sni_hostname"] == "api.example.test" for request in requests
    )
    assert transport.close_calls == 2


def test_probe_rejects_dns_rebinding_before_second_outbound_request() -> None:
    network = _network_module()
    resolver = Resolver(
        ["93.184.216.34"],
        ["93.184.216.34"],
        ["169.254.169.254"],
    )
    request_count = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json={"data": [{"id": "custom-model"}]})

    prober = network.OpenAICompatibleProbe(
        resolver=resolver,
        transport=httpx.MockTransport(handle),
    )

    with pytest.raises(network.ModelEndpointForbidden):
        prober.probe("https://api.example.test/v1", "test-secret-key", "custom-model")

    assert request_count == 1


def test_probe_rejects_redirects_and_never_follows_them() -> None:
    network = _network_module()
    resolver = Resolver(["93.184.216.34"], ["93.184.216.34"])
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"})

    prober = network.OpenAICompatibleProbe(
        resolver=resolver,
        transport=httpx.MockTransport(handle),
    )

    with pytest.raises(network.ModelEndpointForbidden):
        prober.probe("https://api.example.test/v1", "test-secret-key")

    assert len(requests) == 1


def test_runtime_sync_transport_revalidates_and_pins_every_outbound_request() -> None:
    network = _network_module()
    resolver = Resolver(["93.184.216.34"], ["169.254.169.254"])
    requests: list[httpx.Request] = []

    transport = network.PinnedModelTransport(
        base_url="https://api.example.test/v1",
        resolver=resolver,
        transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(200, json={"ok": True})
        ),
    )
    with httpx.Client(transport=transport, follow_redirects=False, trust_env=False) as client:
        response = client.post("https://api.example.test/v1/chat/completions", json={})
        with pytest.raises(network.ModelEndpointForbidden):
            client.post("https://api.example.test/v1/chat/completions", json={})

    assert response.status_code == 200
    assert len(requests) == 1
    assert requests[0].url.host == "93.184.216.34"
    assert requests[0].headers["Host"] == "api.example.test"
    assert requests[0].extensions["sni_hostname"] == "api.example.test"


def test_runtime_async_transport_revalidates_and_pins_every_outbound_request() -> None:
    network = _network_module()
    resolver = Resolver(["93.184.216.34"], ["169.254.169.254"])
    requests: list[httpx.Request] = []

    async def exercise() -> None:
        transport = network.PinnedAsyncModelTransport(
            base_url="https://api.example.test/v1",
            resolver=resolver,
            transport=httpx.MockTransport(
                lambda request: requests.append(request)
                or httpx.Response(200, json={"ok": True})
            ),
        )
        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(
                "https://api.example.test/v1/chat/completions", json={}
            )
            assert response.status_code == 200
            with pytest.raises(network.ModelEndpointForbidden):
                await client.post("https://api.example.test/v1/chat/completions", json={})

    asyncio.run(exercise())

    assert len(requests) == 1
    assert requests[0].url.host == "93.184.216.34"
    assert requests[0].headers["Host"] == "api.example.test"
    assert requests[0].extensions["sni_hostname"] == "api.example.test"


def test_runtime_async_transport_bounds_dns_without_blocking_event_loop() -> None:
    network = _network_module()
    resolver_started = threading.Event()
    release_resolver = threading.Event()
    requests: list[httpx.Request] = []

    def blocking_resolver(_host: str, _port: int) -> Sequence[str]:
        resolver_started.set()
        release_resolver.wait(timeout=0.2)
        return ["93.184.216.34"]

    async def exercise() -> None:
        transport = network.PinnedAsyncModelTransport(
            base_url="https://api.example.test/v1",
            resolver=blocking_resolver,
            resolver_timeout_seconds=0.02,
            transport=httpx.MockTransport(
                lambda request: requests.append(request)
                or httpx.Response(200, json={"ok": True})
            ),
        )
        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            request = asyncio.create_task(
                client.post("https://api.example.test/v1/chat/completions", json={})
            )
            event_loop_ticks = 0

            async def heartbeat() -> None:
                nonlocal event_loop_ticks
                while not request.done():
                    event_loop_ticks += 1
                    await asyncio.sleep(0.001)

            heartbeat_task = asyncio.create_task(heartbeat())
            try:
                while not resolver_started.is_set():
                    await asyncio.sleep(0)
                with pytest.raises(network.ModelProviderUnreachable) as exc_info:
                    await request
            finally:
                release_resolver.set()
                await heartbeat_task

            assert event_loop_ticks > 0
            assert exc_info.value.__cause__ is None
            assert exc_info.value.__context__ is None

    asyncio.run(exercise())

    assert requests == []


def test_runtime_async_transport_timeout_does_not_delay_asyncio_run_shutdown() -> None:
    network = _network_module()
    resolver_started = threading.Event()
    resolver_finished = threading.Event()
    release_resolver = threading.Event()

    def blocking_resolver(_host: str, _port: int) -> Sequence[str]:
        resolver_started.set()
        try:
            release_resolver.wait(timeout=0.4)
            return ["93.184.216.34"]
        finally:
            resolver_finished.set()

    async def exercise() -> None:
        transport = network.PinnedAsyncModelTransport(
            base_url="https://api.example.test/v1",
            resolver=blocking_resolver,
            resolver_timeout_seconds=0.02,
            transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
        )
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            with pytest.raises(network.ModelProviderUnreachable):
                await client.get("https://api.example.test/v1/models")

    started_at = time.perf_counter()
    try:
        asyncio.run(exercise())
        elapsed = time.perf_counter() - started_at
    finally:
        release_resolver.set()
        assert resolver_finished.wait(timeout=1.0)

    assert resolver_started.is_set()
    assert elapsed < 0.15


def test_bounded_resolver_executor_limits_queue_and_shuts_down_without_waiting() -> None:
    network = _network_module()
    resolver_started = threading.Event()
    release_resolver = threading.Event()
    resolver_executor = network._BoundedResolverExecutor(max_workers=1, max_queue_size=1)

    def blocking_resolver() -> str:
        resolver_started.set()
        release_resolver.wait(timeout=1.0)
        return "resolved"

    first = resolver_executor.submit(blocking_resolver)
    assert resolver_started.wait(timeout=1.0)
    queued = resolver_executor.submit(lambda: "queued")
    with pytest.raises(network._ResolverExecutorSaturated):
        resolver_executor.submit(lambda: "overflow")

    started_at = time.perf_counter()
    resolver_executor.shutdown(wait=False, cancel_futures=True)
    shutdown_elapsed = time.perf_counter() - started_at
    try:
        assert shutdown_elapsed < 0.1
    finally:
        release_resolver.set()
        resolver_executor.shutdown(wait=True, cancel_futures=True)

    assert first.result(timeout=1.0) == "resolved"
    assert queued.cancelled()


def test_runtime_async_transport_bounds_repeated_timed_out_resolvers() -> None:
    network = _network_module()
    resolver_started = threading.Event()
    release_resolver = threading.Event()
    resolver_calls = 0
    resolver_executor = network._BoundedResolverExecutor(max_workers=1, max_queue_size=0)

    def blocking_resolver(_host: str, _port: int) -> Sequence[str]:
        nonlocal resolver_calls
        resolver_calls += 1
        resolver_started.set()
        release_resolver.wait(timeout=1.0)
        return ["93.184.216.34"]

    async def exercise() -> list[float]:
        transport = network.PinnedAsyncModelTransport(
            base_url="https://api.example.test/v1",
            resolver=blocking_resolver,
            resolver_timeout_seconds=0.02,
            resolver_executor=resolver_executor,
            transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
        )
        elapsed_requests: list[float] = []
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            for _ in range(4):
                started_at = time.perf_counter()
                with pytest.raises(network.ModelProviderUnreachable) as exc_info:
                    await client.get("https://api.example.test/v1/models")
                elapsed_requests.append(time.perf_counter() - started_at)
                assert exc_info.value.__cause__ is None
                assert exc_info.value.__context__ is None
        return elapsed_requests

    try:
        elapsed_requests = asyncio.run(exercise())
    finally:
        release_resolver.set()
        resolver_executor.shutdown(wait=True, cancel_futures=True)

    assert resolver_started.is_set()
    assert resolver_calls == 1
    assert max(elapsed_requests) < 0.15


def test_runtime_client_never_follows_provider_redirects() -> None:
    network = _network_module()
    requests: list[httpx.Request] = []
    transport = network.PinnedModelTransport(
        base_url="https://api.example.test/v1",
        resolver=Resolver(["93.184.216.34"]),
        transport=httpx.MockTransport(
            lambda request: requests.append(request)
            or httpx.Response(
                307,
                headers={"Location": "https://api.example.test/internal"},
            )
        ),
    )

    with httpx.Client(transport=transport, follow_redirects=False, trust_env=False) as client:
        response = client.post("https://api.example.test/v1/chat/completions", json={})

    assert response.status_code == 307
    assert len(requests) == 1


def test_probe_bounds_and_sorts_model_ids() -> None:
    network = _network_module()
    resolver = Resolver(["93.184.216.34"], ["93.184.216.34"])
    ids = [f"model-{index:03}" for index in range(network.MAX_PROBE_MODELS + 25)]
    ids.extend(["model-001", "", "x" * (network.MAX_MODEL_ID_LENGTH + 1)])
    prober = network.OpenAICompatibleProbe(
        resolver=resolver,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": [
            {"id": model_id} for model_id in reversed(ids)
        ]})),
    )

    result = prober.probe("https://api.example.test/v1", "test-secret-key")

    assert result.models == tuple(sorted(set(ids[: network.MAX_PROBE_MODELS])))
    assert result.models_truncated is True


@pytest.mark.parametrize(
    ("response", "error_name"),
    [
        (httpx.Response(401), "ModelAuthenticationFailed"),
        (httpx.Response(403), "ModelAuthenticationFailed"),
        (httpx.Response(500, content=b"remote-secret-body"), "ModelProbeFailed"),
        (httpx.Response(200, content=b"not-json remote-secret-body"), "ModelProbeFailed"),
    ],
)
def test_probe_maps_remote_failures_without_exposing_body(
    response: httpx.Response, error_name: str
) -> None:
    network = _network_module()
    prober = network.OpenAICompatibleProbe(
        resolver=Resolver(["93.184.216.34"], ["93.184.216.34"]),
        transport=httpx.MockTransport(lambda request: response),
    )

    with pytest.raises(getattr(network, error_name)) as exc_info:
        prober.probe("https://api.example.test/v1", "test-secret-key")

    assert "test-secret-key" not in str(exc_info.value)
    assert "remote-secret-body" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_probe_maps_timeouts_and_protocol_errors_to_unreachable() -> None:
    network = _network_module()

    for exception in (
        httpx.ConnectTimeout("connect timed out"),
        httpx.ReadTimeout("read timed out"),
        httpx.RemoteProtocolError("remote-secret-protocol-detail"),
    ):
        prober = network.OpenAICompatibleProbe(
            resolver=Resolver(["93.184.216.34"], ["93.184.216.34"]),
            transport=httpx.MockTransport(
                lambda request, exc=exception: (_ for _ in ()).throw(exc)
            ),
        )
        with pytest.raises(network.ModelProviderUnreachable) as exc_info:
            prober.probe("https://api.example.test/v1", "test-secret-key")
        assert "remote-secret-protocol-detail" not in str(exc_info.value)
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__context__ is None


def test_probe_rejects_oversized_response_without_exposing_it() -> None:
    network = _network_module()
    body = b"s" * (network.MAX_PROBE_RESPONSE_BYTES + 1)
    prober = network.OpenAICompatibleProbe(
        resolver=Resolver(["93.184.216.34"], ["93.184.216.34"]),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)),
    )

    with pytest.raises(network.ModelProbeFailed) as exc_info:
        prober.probe("https://api.example.test/v1", "test-secret-key")

    assert "ssss" not in str(exc_info.value)


def test_probe_requests_identity_and_rejects_compression_before_decoding() -> None:
    network = _network_module()
    compressed = gzip.compress(b"x" * (network.MAX_PROBE_RESPONSE_BYTES * 8))
    stream = TrackingStream(compressed)
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=stream,
        )

    prober = network.OpenAICompatibleProbe(
        resolver=Resolver(["93.184.216.34"], ["93.184.216.34"]),
        transport=httpx.MockTransport(handle),
    )

    with pytest.raises(network.ModelProbeFailed):
        prober.probe("https://api.example.test/v1", "test-secret-key")

    assert requests[0].headers["Accept-Encoding"] == "identity"
    assert stream.iterations == 0


def test_probe_rejects_error_status_without_reading_response_body() -> None:
    network = _network_module()
    stream = TrackingStream(b"x" * (network.MAX_PROBE_RESPONSE_BYTES + 1))
    prober = network.OpenAICompatibleProbe(
        resolver=Resolver(["93.184.216.34"], ["93.184.216.34"]),
        transport=httpx.MockTransport(lambda request: httpx.Response(401, stream=stream)),
    )

    with pytest.raises(network.ModelAuthenticationFailed):
        prober.probe("https://api.example.test/v1", "test-secret-key")

    assert stream.iterations == 0


def test_probe_rejects_malformed_successful_completion() -> None:
    network = _network_module()
    resolver = Resolver(
        ["93.184.216.34"],
        ["93.184.216.34"],
        ["93.184.216.34"],
    )

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"choices": [{}]})

    prober = network.OpenAICompatibleProbe(
        resolver=resolver,
        transport=httpx.MockTransport(handle),
    )

    with pytest.raises(network.ModelProbeFailed):
        prober.probe("https://api.example.test/v1", "test-secret-key", "custom-model")
