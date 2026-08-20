import threading
from types import SimpleNamespace

import pytest
from grpc import RpcError, StatusCode
from pydantic import ValidationError

from app.brokers import InstrumentInfo, StopOrder, TradingError
from app.brokers.finam import FinamApiClient, FinamBrokerService, FinamConfig


class FakeRpcError(RpcError):
    def __init__(self, status_code: StatusCode, details: str = "temporary failure") -> None:
        super().__init__()
        self._status_code = status_code
        self._details = details

    def code(self):
        return self._status_code

    def details(self):
        return self._details


class FakeUnaryCall:
    _method = b"/finam.test.Service/Read"

    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = []

    def with_call(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, object()


def make_client(
    *, timeout: float = 5.0, max_attempts: int = 20, non_retryable_timeout: float = 30.0
) -> FinamApiClient:
    client = object.__new__(FinamApiClient)
    client.channel = None
    client.request_timeout = timeout
    client.request_max_attempts = max_attempts
    client.non_retryable_request_timeout = non_retryable_timeout
    client._auth_lock = threading.RLock()
    return client


@pytest.fixture(autouse=True)
def disable_retry_sleep(monkeypatch):
    monkeypatch.setattr("app.brokers.finam.time.sleep", lambda _: None)


@pytest.fixture
def mutation_service():
    service = object.__new__(FinamBrokerService)
    service.config = FinamConfig(token="token", account_id="account")
    service._client = SimpleNamespace(
        orders_stub=SimpleNamespace(
            PlaceOrder=object(),
            CancelOrder=object(),
        )
    )
    calls = []

    def call_function(func, request, **kwargs):
        calls.append((func, request, kwargs))
        return SimpleNamespace(order_id="order-id")

    service.call_function = call_function
    return service, calls


@pytest.fixture
def instrument():
    return InstrumentInfo(
        instrument="SBER@MISX",
        name="Sberbank",
        type="share",
        currency="RUB",
        lot_size=10,
        min_price_step=0.01,
    )


def test_request_policy_defaults():
    config = FinamConfig(token="token", account_id="account")

    assert config.request_timeout == 5.0
    assert config.request_max_attempts == 20
    assert config.non_retryable_request_timeout == 30.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_timeout", 0),
        ("request_max_attempts", 0),
        ("non_retryable_request_timeout", 0),
    ],
)
def test_request_policy_must_be_positive(field, value):
    with pytest.raises(ValidationError):
        FinamConfig(token="token", account_id="account", **{field: value})


def test_read_rpc_retries_with_configured_timeout():
    client = make_client(timeout=10.0, max_attempts=10)
    rpc = FakeUnaryCall([
        FakeRpcError(StatusCode.DEADLINE_EXCEEDED),
        FakeRpcError(StatusCode.UNAVAILABLE),
        SimpleNamespace(value="ok"),
    ])

    response = client._call_rpc(
        rpc,
        request=object(),
        metadata=(("authorization", "jwt"),),
        max_attempts=client.request_max_attempts,
    )

    assert response.value == "ok"
    assert len(rpc.calls) == 3
    assert all(call["timeout"] == 10.0 for call in rpc.calls)


def test_read_rpc_raises_after_max_attempts():
    client = make_client(max_attempts=3)
    rpc = FakeUnaryCall([
        FakeRpcError(StatusCode.DEADLINE_EXCEEDED),
        FakeRpcError(StatusCode.DEADLINE_EXCEEDED),
        FakeRpcError(StatusCode.DEADLINE_EXCEEDED),
    ])

    with pytest.raises(TradingError) as error:
        client._call_rpc(
            rpc,
            request=object(),
            max_attempts=client.request_max_attempts,
        )

    assert error.value.code == "FINAM_REQUEST_ERROR"
    assert len(rpc.calls) == 3


def test_non_retryable_call_is_attempted_once(monkeypatch):
    client = make_client(max_attempts=20, non_retryable_timeout=30.0)
    monkeypatch.setattr(client, "auth", lambda: None)
    client.metadata = ("authorization", "jwt")
    rpc = FakeUnaryCall([FakeRpcError(StatusCode.DEADLINE_EXCEEDED)])

    with pytest.raises(TradingError):
        client.call_function(rpc, request=object(), retry=False)

    assert len(rpc.calls) == 1
    assert rpc.calls[0]["timeout"] == 30.0


def test_auth_uses_timeout_and_retry_policy():
    client = make_client(timeout=7.5, max_attempts=2)
    client.access_token = "access-token"
    client.jwt_token = ""
    client.jwt_token_issued = 0
    client.jwt_token_ttl = 15 * 60
    auth_rpc = FakeUnaryCall([
        FakeRpcError(StatusCode.UNAVAILABLE),
        SimpleNamespace(token="jwt-token"),
    ])
    client.auth_stub = SimpleNamespace(Auth=auth_rpc)

    client.auth()

    assert client.metadata == ("authorization", "jwt-token")
    assert len(auth_rpc.calls) == 2
    assert all(call["timeout"] == 7.5 for call in auth_rpc.calls)


def test_all_order_mutations_disable_retries(mutation_service, instrument):
    service, calls = mutation_service
    service.place_market_order(instrument, "buy", 1)
    service.place_stop_loss_order(instrument, "sell", 1, 100.0)
    service.place_take_profit_order(instrument, "sell", 1, 120.0)
    service.cancel_stop_orders([
        StopOrder(
            order_id="stop-order-id",
            order_type="stop_loss",
            direction="sell",
            quantity=1,
        )
    ])

    assert len(calls) == 4
    assert all(kwargs == {"retry": False} for _, _, kwargs in calls)
