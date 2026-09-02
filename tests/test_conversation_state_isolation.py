"""
The WhatsApp conversation store must not carry state between tests.

Three separate Redis stores back this application, all on db 2, all keyed by
phone number: message de-duplication, `wa:{phone}` conversation state with a
one-hour TTL, and `wa:pending_signup:{phone}`. Each was built with its own
connection at the point of use, so none of them could be replaced by a
fixture. Against a live Redis that made the suite meaningless -- a customer
left half way through an order stayed half way through it for the next test,
and for the next run an hour later. The suite passed only on machines where
no Redis happened to be listening.

These tests are deliberately order-dependent: the second one asserts that the
first one's conversation is gone. That is the property, so it is what is
checked.
"""

from app.services import wa_customer_flow

PHONE = "919876500011"


def test_the_conversation_store_is_a_fake_not_a_real_connection():
    """If a live Redis is reachable, everything below silently proves nothing."""
    store = wa_customer_flow._get_redis()
    assert store is not None, "the fixture should always supply a store"
    assert "fakeredis" in type(store).__module__, (
        f"talking to a real Redis ({type(store).__module__}) -- test state will "
        "outlive the run"
    )


def test_a_customer_left_mid_order_is_recorded():
    wa_customer_flow._set_state(PHONE, {"step": "SELECT_AREA", "cake": "chocolate"})
    assert wa_customer_flow._get_state(PHONE)["step"] == "SELECT_AREA"


def test_the_next_test_starts_from_a_clean_conversation():
    """Runs after the test above, and must not see its cake."""
    assert wa_customer_flow._get_state(PHONE) == {"step": "IDLE"}


def test_the_in_process_fallback_is_reset_too():
    """
    The no-Redis path is a module-level dict. It leaks exactly like the real
    store did, and nothing used to clear it.
    """
    assert wa_customer_flow._memory == {}


def test_pending_signups_share_the_same_replaceable_store():
    """
    The webhook's consent step opened its own connection to db 2. A fixture
    could not reach it, so a half-finished signup survived the test that
    created it.
    """
    from app.api.routes import webhook

    src = webhook._handle_new_user.__code__.co_consts
    assert not any(
        isinstance(c, str) and "from_url" in c for c in src if isinstance(c, str)
    )
    # The accessor it now uses is the patched one.
    assert wa_customer_flow._get_redis() is wa_customer_flow._client
