import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import test_applications as m2
import test_m3 as m3
import test_m4 as m4
from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.applications import workflow
from app.applications.models import Badge, CreatorApplication
from app.config import settings
from app.events.cleanup import cleanup_event
from app.events.models import Event
from app.helpers import service as helpers
from app.helpers.models import HelperInvitation, HelperRegistration
from app.identity.models import ExternalIdentity
from app.registration.client import (
    RegistrationResult,
    RegistrationStatus,
    RegistrationUnavailable,
)
from app.registration.evidence import (
    record_verified,
    stored_snapshot,
    verify_participant,
)
from app.registration.models import RegistrationVerification
from app.staff import exports

application_engine = m2.application_engine
postgres_engine = m2.postgres_engine
browser = m2.browser
run = asyncio.run


class NoRegistration:
    async def lookup(self, lookup):
        pytest.fail("Later workflows must not call Registration")


def test_verification_once_per_role_and_event_and_immutable_later_workflows(
    application_engine,
):
    with Session(application_engine, expire_on_commit=False) as db:
        creator = m3.approve(db)
        first = db.scalar(select(RegistrationVerification))
        original = (
            first.reg_id,
            first.issuer,
            first.subject,
            first.verified_at,
            first.status,
        )
        assert original[:3] == ("REG-123", settings.oidc_issuer_url, "person-1")
        assert (
            first.event_id == 1
            and first.event_year == 2028
            and first.purpose == "creator"
        )
        _, invitation = run(
            helpers.invite(db, 1, creator, NoRegistration(), m2.IdentityFake())
        )
        failed = m2.RegistrationFake()
        failed.status = RegistrationStatus.INELIGIBLE
        with pytest.raises(HTTPException) as error:
            run(helpers.redeem(db, 3, invitation, failed, m2.IdentityFake()))
        assert error.value.status_code == 409
        assert db.scalar(select(HelperRegistration)) is None
        assert db.scalar(select(HelperInvitation)).consumed_at is None
        helper = run(
            helpers.redeem(db, 3, invitation, m2.RegistrationFake(), m2.IdentityFake())
        )
        run(
            helpers.decide(
                db, 1, helper, 1, "CONFIRMED", NoRegistration(), m2.IdentityFake()
            )
        )
        run(
            helpers.decide(
                db, 1, helper, 2, "DECLINED", NoRegistration(), m2.IdentityFake()
            )
        )
        run(
            helpers.decide(
                db, 1, helper, 3, "CONFIRMED", NoRegistration(), m2.IdentityFake()
            )
        )
        run(
            helpers.withdraw(
                db, 3, helper, 4, NoRegistration(), m2.IdentityFake(), helper=True
            )
        )
        run(
            helpers.withdraw(
                db,
                2,
                helper,
                5,
                NoRegistration(),
                m2.IdentityFake(),
                helper=True,
                restore=True,
                administrative=True,
                reason="Restore",
            )
        )
        assert db.scalar(select(func.count()).select_from(Badge)) == 2
        db.refresh(first)
        assert (
            first.reg_id,
            first.issuer,
            first.subject,
            first.verified_at,
            first.status,
        ) == original
        assert (
            stored_snapshot(db, 1, "helper") is None
        )  # Creator proof never grants helper eligibility.
        assert stored_snapshot(db, 3, "creator") is None
        assert stored_snapshot(db, 3, "helper") is not None
        # Helping a second creator reuses that user's separately verified helper evidence.
        other = m3.approve(db, 2)
        _, secret = run(
            helpers.invite(db, 2, other, NoRegistration(), m2.IdentityFake())
        )
        run(helpers.redeem(db, 3, secret, NoRegistration(), m2.IdentityFake()))
        assert (
            db.scalar(select(func.count()).select_from(RegistrationVerification)) == 3
        )


@pytest.mark.parametrize(
    "status", [RegistrationStatus.INELIGIBLE, RegistrationStatus.UNKNOWN, None]
)
def test_retry_after_failure_never_caches_negative_evidence(application_engine, status):
    with Session(application_engine) as db:
        fake = m2.RegistrationFake()
        fake.status = status
        for _ in range(2):
            with pytest.raises(HTTPException):
                run(workflow.submit(db, 1, m2.input_data(), fake, m2.IdentityFake()))
            assert db.scalar(select(RegistrationVerification)) is None
            assert db.scalar(select(CreatorApplication)) is None
        fake.status = RegistrationStatus.CHECKED_IN
        key = run(workflow.submit(db, 1, m2.input_data(), fake, m2.IdentityFake()))
        assert fake.calls == 3
        assert db.scalar(select(RegistrationVerification.status)) == "CHECKED_IN"
        assert (
            run(
                workflow.submit(
                    db, 1, m2.input_data(), NoRegistration(), m2.IdentityFake()
                )
            )
            == key
        )


@pytest.mark.parametrize("reg_id", [None, "", " ", "bad\nvalue", 123])
def test_paid_without_valid_registration_id_never_creates_evidence(
    application_engine, reg_id
):
    class MissingRegistration:
        async def lookup(self, lookup):
            return RegistrationResult(lookup, RegistrationStatus.PAID, reg_id)

    with Session(application_engine) as db:
        with pytest.raises(HTTPException) as error:
            run(
                workflow.submit(
                    db, 1, m2.input_data(), MissingRegistration(), m2.IdentityFake()
                )
            )
        assert error.value.status_code == 503
        assert db.scalar(select(RegistrationVerification)) is None


@pytest.mark.parametrize(
    "change",
    ["identity", "ambiguous", "user", "event", "reg_id", "timestamp", "missing"],
)
def test_later_actions_fail_closed_on_unbound_evidence(application_engine, change):
    with Session(application_engine, expire_on_commit=False) as db:
        key, store = m4.complete_creator(db)
        record = db.get(CreatorApplication, key)
        row = db.scalar(select(RegistrationVerification))
        if change == "identity":
            row.subject = "someone-else"
        elif change == "ambiguous":
            db.add(
                ExternalIdentity(
                    user_id=1, issuer=settings.oidc_issuer_url, subject="second"
                )
            )
        elif change == "user":
            row.user_id = 2
        elif change == "event":
            db.get(Event, 1).year += 1
        elif change == "reg_id":
            record.reg_id = "different"
        elif change == "timestamp":
            record.eligibility_checked_at += timedelta(seconds=1)
        else:
            db.delete(row)
        db.commit()
        assert not record_verified(db, record, "creator")
        with pytest.raises(HTTPException) as error:
            run(exports.export(db, 2, 1, NoRegistration(), store))
        assert error.value.status_code == 409
        assert "Registration verification is missing" in str(error.value.detail)
        assert db.get(CreatorApplication, key).status == "APPROVED"
        assert db.scalar(select(Badge.badge_number)) == 1


def test_initial_verification_revalidates_identity_after_external_io(
    application_engine,
):
    with Session(application_engine) as db:
        fake = m2.RegistrationFake()

        def change():
            assert not db.in_transaction()
            with Session(application_engine) as other:
                other.scalar(
                    select(ExternalIdentity).where(ExternalIdentity.user_id == 1)
                ).subject = "changed"
                other.commit()

        fake.hook = change
        with pytest.raises(HTTPException, match="identity changed"):
            run(workflow.submit(db, 1, m2.input_data(), fake, m2.IdentityFake()))
        assert db.scalar(select(RegistrationVerification)) is None
        assert db.scalar(select(CreatorApplication)) is None


def test_legacy_verification_is_explicit_retryable_and_preserves_workflow(
    application_engine,
):
    with Session(application_engine) as db:
        key, store = m4.complete_creator(db)
        db.execute(delete(RegistrationVerification))
        db.commit()
        failure = m2.RegistrationFake()
        failure.failure = RegistrationUnavailable()
        with pytest.raises(HTTPException):
            run(verify_participant(db, 1, "creator", failure, m2.IdentityFake()))
        assert db.get(CreatorApplication, key).status == "APPROVED"
        run(
            verify_participant(
                db, 1, "creator", m2.RegistrationFake(), m2.IdentityFake()
            )
        )
        assert record_verified(db, db.get(CreatorApplication, key), "creator")
        assert run(exports.export(db, 2, 1, NoRegistration(), store))
        assert db.scalar(select(Badge.badge_number)) == 1
        run(verify_participant(db, 1, "creator", NoRegistration(), m2.IdentityFake()))


def test_verification_only_participant_is_removed_with_event(application_engine):
    with Session(application_engine) as db:
        run(
            verify_participant(
                db, 1, "helper", m2.RegistrationFake(), m2.IdentityFake()
            )
        )
        assert db.scalar(select(RegistrationVerification)) is not None
        db.get(Event, 1).data_delete_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
        assert cleanup_event(db, m2.SubmissionStore(), 1)
        assert db.scalar(select(RegistrationVerification)) is None


def test_verification_page_csrf_retry_and_no_provider_call_on_status(browser):
    client, registration, engine = browser
    path = "/auth/registration?purpose=creator"
    assert (
        client.post("/auth/registration", data={"purpose": "creator"}).status_code
        == 403
    )
    registration.status = RegistrationStatus.INELIGIBLE
    response = client.post(
        "/auth/registration",
        data={"purpose": "creator", "csrf_token": m2.csrf(client, path)},
    )
    assert "Confirm payment" in response.text and "Verify Registration" in response.text
    registration.status = RegistrationStatus.PAID
    response = client.post(
        "/auth/registration",
        data={"purpose": "creator", "csrf_token": m2.csrf(client, path)},
    )
    assert "Registration verified" in response.text
    calls = registration.calls
    assert client.get("/applications/eligibility").json() == {
        "status": "ELIGIBLE",
        "retryable": False,
    }
    assert m2.submit_form(client).status_code == 303
    assert registration.calls == calls
    with Session(engine) as db:
        assert record_verified(db, db.scalar(select(CreatorApplication)), "creator")
