from __future__ import annotations

import json
from collections.abc import Sequence
from importlib import import_module

import httpx
import pytest


def _network_module():
    try:
        return import_module("services.model_config_network")
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

    prober = network.OpenAICompatibleProbe(
        resolver=resolver,
        transport=httpx.MockTransport(handle),
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
