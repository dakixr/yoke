"""Reuse immutable validation adapters across isolated HTTP applications."""

from __future__ import annotations

import pickle
from typing import Any

import pytest
from fastapi._compat.v2 import ModelField, asdict
from fastapi.dependencies import utils as dependency_utils
from fastapi import routing


@pytest.fixture(scope="session")
def http_field_adapters() -> dict[bytes, Any]:
    return {}


@pytest.fixture(autouse=True)
def reuse_http_validation_schemas(
    monkeypatch: pytest.MonkeyPatch, http_field_adapters: dict[bytes, Any]
) -> None:
    """Keep fields and applications separate; share only identical type adapters.

    FastAPI rebuilds Pydantic adapters for every application even though route
    annotations and constraints are static. Serialize every field attribute,
    its annotation, metadata and config to distinguish aliases, defaults and
    validation rules. Local/dynamic annotations that cannot be serialized use
    the normal constructor. Applications, dependencies and route state are
    never shared.
    """
    initialize = ModelField.__post_init__

    def initialize_field(field: ModelField) -> None:
        try:
            key = pickle.dumps((asdict(field.field_info), field.config))
        except (AttributeError, TypeError, pickle.PicklingError):
            initialize(field)
            return
        if key not in http_field_adapters:
            initialize(field)
            http_field_adapters[key] = field._type_adapter
        else:
            field._type_adapter = http_field_adapters[key]

    monkeypatch.setattr(ModelField, "__post_init__", initialize_field)


@pytest.fixture(scope="session")
def http_dependency_graphs() -> dict[bytes, Any]:
    return {}


@pytest.fixture(autouse=True)
def reuse_http_dependency_graphs(
    monkeypatch: pytest.MonkeyPatch, http_dependency_graphs: dict[bytes, Any]
) -> None:
    """Share static Yoke route metadata, never the request's dependency values.

    These functions resolve services from request.app.state. Their dependency
    graph contains annotations/validators, not an application or a service.
    Include all path/scope/options in the key. Other callables and dynamic
    overrides still construct their own graphs normally.
    """
    build = dependency_utils.get_dependant

    def graph(**options):
        call = options["call"]
        if not getattr(call, "__module__", "").startswith("yoke.http."):
            return build(**options)
        try:
            key = pickle.dumps(options)
        except (AttributeError, TypeError, pickle.PicklingError):
            return build(**options)
        if key not in http_dependency_graphs:
            http_dependency_graphs[key] = build(**options)
        return http_dependency_graphs[key]

    monkeypatch.setattr(dependency_utils, "get_dependant", graph)
    monkeypatch.setattr(routing, "get_dependant", graph)
