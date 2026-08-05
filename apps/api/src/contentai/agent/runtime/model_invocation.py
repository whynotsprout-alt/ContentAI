from __future__ import annotations

import inspect
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler


class InvocationAttempt(BaseCallbackHandler):
    """Track whether a model attempt emitted output before it failed."""

    run_inline = True

    def __init__(self) -> None:
        super().__init__()
        self.emitted_tokens = False
        self.output_observable = True

    def on_llm_new_token(self, token: Any, **_kwargs: Any) -> None:
        _ = token
        self.emitted_tokens = True

    @property
    def replay_safe(self) -> bool:
        """Whether a transport failure can be retried without duplicating output."""
        return self.output_observable and not self.emitted_tokens

    def mark_output_unobservable(self) -> None:
        """Conservatively disable replay when a model cannot receive callbacks."""
        self.output_observable = False


def append_callback(callbacks: Any, handler: BaseCallbackHandler) -> Any:
    """Add a handler without mutating a caller-owned callback collection."""
    if callbacks is None:
        return [handler]
    copy = getattr(callbacks, "copy", None)
    if callable(copy) and hasattr(callbacks, "add_handler"):
        copied = copy()
        copied.add_handler(handler)
        return copied
    if isinstance(callbacks, list | tuple):
        return [*callbacks, handler]
    return [callbacks, handler]


def invoke_model(
    model: Any,
    messages: Any,
    *,
    callbacks: Any = None,
    include_empty_callbacks: bool = False,
    timeout_seconds: float | None = None,
) -> Any:
    """Invoke a sync model while supporting small provider-compatible test doubles."""
    signature_invoke = _signature_invoke(model, "invoke")
    invoke_kwargs = _timeout_kwargs(signature_invoke, timeout_seconds)
    if callbacks is None and not include_empty_callbacks:
        return model.invoke(messages, **invoke_kwargs)
    config_support = _supports_keyword(signature_invoke, "config")
    if config_support is False:
        _mark_attempts_unobservable(callbacks)
        return model.invoke(messages, **invoke_kwargs)
    if config_support is None:
        # A variadic or opaque callable can accept ``config`` while silently
        # ignoring it. Keep the one-call compatibility path, but do not let an
        # outer transport retry assume token callbacks were observable.
        _mark_attempts_unobservable(callbacks)
    # When the signature cannot be inspected, make exactly one call. Retrying
    # after a heuristic TypeError can replay a request that already streamed
    # output through the supplied callbacks.
    return model.invoke(
        messages,
        config={"callbacks": callbacks or []},
        **invoke_kwargs,
    )


async def ainvoke_model(
    model: Any,
    messages: Any,
    *,
    callbacks: Any = None,
    include_empty_callbacks: bool = False,
    timeout_seconds: float | None = None,
) -> Any:
    """Invoke an async model with the same compatibility behavior as ``invoke_model``."""
    signature_invoke = _signature_invoke(model, "ainvoke")
    invoke_kwargs = _timeout_kwargs(signature_invoke, timeout_seconds)
    if callbacks is None and not include_empty_callbacks:
        return await model.ainvoke(messages, **invoke_kwargs)
    config_support = _supports_keyword(signature_invoke, "config")
    if config_support is False:
        _mark_attempts_unobservable(callbacks)
        return await model.ainvoke(messages, **invoke_kwargs)
    if config_support is None:
        _mark_attempts_unobservable(callbacks)
    return await model.ainvoke(
        messages,
        config={"callbacks": callbacks or []},
        **invoke_kwargs,
    )


def _timeout_kwargs(invoke: Any, timeout_seconds: float | None) -> dict[str, float]:
    if timeout_seconds is None:
        return {}
    try:
        parameters = inspect.signature(invoke).parameters
    except (TypeError, ValueError):
        return {}
    declared = parameters.get("timeout")
    if declared is not None:
        if declared.kind not in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }:
            return {}
    elif not any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return {}
    return {"timeout": max(0.1, float(timeout_seconds))}


def _signature_invoke(model: Any, method_name: str) -> Any:
    """Resolve the callable whose signature a transparent model proxy preserves."""
    invoke = getattr(model, method_name)
    try:
        unwrapped_model = inspect.unwrap(model)
    except ValueError:
        return invoke
    unwrapped_invoke = getattr(unwrapped_model, method_name, None)
    return unwrapped_invoke if callable(unwrapped_invoke) else invoke


def _supports_keyword(invoke: Any, keyword: str) -> bool | None:
    """Return explicit support, rejection, or ambiguous variadic support."""
    try:
        parameters = inspect.signature(invoke).parameters
    except (TypeError, ValueError):
        return None
    declared = parameters.get(keyword)
    if declared is not None and declared.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }:
        return True
    if declared is not None:
        # An explicit positional-only parameter keeps its positional binding
        # even when the callable also exposes **kwargs with the same spelling.
        return False
    if any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return None
    return False


def _mark_attempts_unobservable(callbacks: Any) -> None:
    """Mark retry guards nested in common callback collections as conservative."""
    pending = [callbacks]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, InvocationAttempt):
            current.mark_output_unobservable()
            continue
        if isinstance(current, list | tuple | set | frozenset):
            pending.extend(current)
            continue
        for attribute in ("handlers", "inheritable_handlers"):
            nested = getattr(current, attribute, None)
            if nested is not None:
                pending.append(nested)


__all__ = ["InvocationAttempt", "ainvoke_model", "append_callback", "invoke_model"]
