import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import BytesIO
from threading import Barrier

import pytest
import test_applications as m2
from fastapi import HTTPException
from PIL import Image, PngImagePlugin
from sqlalchemy import event as sql_event
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_applications import (
    IdentityFake,
    RegistrationFake,
    csrf,
    input_data,
    login,
)

from app.applications import workflow
from app.applications.models import (
    Badge,
    BadgeCounter,
    BusinessAudit,
    CreatorApplication,
    LocalRoleAssignment,
    NotificationOutbox,
)
from app.config import settings
from app.creators import images, service
from app.creators.models import BannedChannel, CreatorProfile, ProfileImage
from app.creators.policy import change_allowed, helper_active
from app.events.models import Event
from app.helpers import service as helpers
from app.helpers.models import HelperInvitation, HelperRegistration
from app.registration.client import RegistrationStatus, RegistrationUnavailable

application_engine = m2.application_engine
browser = m2.browser
postgres_engine = m2.postgres_engine


def run(coroutine):
    return asyncio.run(coroutine)


def approve(db, user_id=1):
    application_id = run(
        workflow.submit(
            db,
            user_id,
            input_data(f"@creator{user_id}"),
            RegistrationFake(),
            IdentityFake(),
        )
    )
    record = db.get(CreatorApplication, application_id)
    run(
        workflow.review(
            db,
            2,
            application_id,
            record.version,
            "ON_REVIEW",
            "",
            "",
            False,
            RegistrationFake(),
            IdentityFake(),
        )
    )
    record = db.get(CreatorApplication, application_id)
    run(
        workflow.review(
            db,
            2,
            application_id,
            record.version,
            "APPROVED",
            "",
            "",
            False,
            RegistrationFake(),
            IdentityFake(),
        )
    )
    return application_id


def invite(db, application_id, actor=1, **kwargs):
    return run(
        helpers.invite(
            db, actor, application_id, RegistrationFake(), IdentityFake(), **kwargs
        )
    )


def register(db, application_id, user=3, actor=1):
    _, secret = invite(db, application_id, actor)
    return run(helpers.redeem(db, user, secret, RegistrationFake(), IdentityFake()))


def decide(db, helper_id, target="CONFIRMED", actor=1, **kwargs):
    version = db.get(HelperRegistration, helper_id).version
    return run(
        helpers.decide(
            db,
            actor,
            helper_id,
            version,
            target,
            RegistrationFake(),
            IdentityFake(),
            **kwargs,
        )
    )


def withdraw(db, entity_id, *, actor=1, helper=False, **kwargs):
    record = db.get(HelperRegistration if helper else CreatorApplication, entity_id)
    return run(
        helpers.withdraw(
            db,
            actor,
            entity_id,
            record.version,
            RegistrationFake(),
            IdentityFake(),
            helper=helper,
            **kwargs,
        )
    )


def png(*, size=(600, 600), format="PNG", dpi=(72, 72)):
    output = BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private", "must not survive")
    Image.new("RGB", size, "red").save(output, format=format, dpi=dpi, pnginfo=metadata)
    return output.getvalue()


class StoreFake:
    def __init__(self, db):
        self.db = db
        self.objects = {}
        self.fail_put = self.fail_delete = False
        self.hook = None

    def put(self, key, data):
        assert not self.db.in_transaction()
        if self.fail_put:
            raise images.ImageStorageUnavailable
        self.objects[key] = data
        if self.hook:
            self.hook()

    def get(self, key):
        assert not self.db.in_transaction()
        return self.objects[key]

    def delete(self, key):
        assert not self.db.in_transaction()
        if self.fail_delete:
            raise images.ImageStorageUnavailable
        self.objects.pop(key, None)


@pytest.fixture
def approved(application_engine):
    with Session(application_engine, expire_on_commit=False) as db:
        application_id = approve(db)
        yield db, application_id


def test_profile_edit_and_banned_warning(approved):
    db, key = approved
    now = datetime.now(UTC)
    db.add(
        BannedChannel(
            platform="Twitch",
            normalized_account="creator1",
            private_reason="test",
            created_at=now,
            updated_at=now,
        )
    )
    db.commit()
    version = db.get(CreatorApplication, key).version
    service.save_profile(
        db, 1, key, version, " One creator name ", input_data("@creator1").channels
    )
    assert db.get(CreatorProfile, key).channel_name == "One creator name"
    assert db.get(CreatorApplication, key).status == "APPROVED"
    assert len(service.banned_matches(db, key)) == 1
    assert (
        db.scalar(
            select(BusinessAudit).where(BusinessAudit.action == "profile_changed")
        ).changes["banned_matches"]
        == 1
    )


@pytest.mark.parametrize(
    "actor,name,code",
    [(3, "name", 404), (1, "", 422), (1, "x" * 201, 422), (1, "line\nname", 422)],
)
def test_profile_validation_and_ownership(approved, actor, name, code):
    db, key = approved
    with pytest.raises(HTTPException) as error:
        service.save_profile(
            db,
            actor,
            key,
            db.get(CreatorApplication, key).version,
            name,
            input_data().channels,
        )
    assert error.value.status_code == code


def test_profile_requires_approval_and_active_participation(application_engine):
    with Session(application_engine) as db:
        key = run(
            workflow.submit(db, 1, input_data(), RegistrationFake(), IdentityFake())
        )
        with pytest.raises(HTTPException, match="active approved"):
            service.save_profile(db, 1, key, 1, "name", input_data().channels)


def test_image_normalization_and_invalid_inputs():
    output = images.normalize_png(png())
    with Image.open(BytesIO(output)) as image:
        assert image.format == "PNG" and image.size == (600, 600)
        assert image.info["dpi"] == pytest.approx((300, 300), abs=0.01)
        assert "private" not in image.info
    for data in (png(size=(599, 600)), png(format="JPEG"), b"not a png", png()[:50]):
        with pytest.raises(HTTPException) as error:
            images.normalize_png(data)
        assert error.value.status_code == 422


def test_image_size_cap(monkeypatch):
    data = png()
    monkeypatch.setattr(settings, "profile_image_max_bytes", len(data) - 1)
    with pytest.raises(HTTPException) as error:
        images.normalize_png(data)
    assert error.value.status_code == 413


def test_image_replace_and_orphan_cleanup(approved):
    db, key = approved
    store = StoreFake(db)
    images.replace_picture(
        db, 1, key, db.get(CreatorApplication, key).version, png(), store
    )
    first = db.get(CreatorProfile, key).image_id
    old_key = db.get(ProfileImage, first).object_key
    assert old_key.startswith("profiles/") and len(store.objects) == 1
    store.fail_delete = True
    images.replace_picture(
        db, 1, key, db.get(CreatorApplication, key).version, png(), store
    )
    second = db.get(CreatorProfile, key).image_id
    assert first != second and db.get(ProfileImage, first).deletion_failed
    assert len(store.objects) == 2
    store.fail_delete = False
    assert images.cleanup_images(db, store) == 0
    assert old_key not in store.objects and len(store.objects) == 1


@pytest.mark.parametrize("failure", ["storage", "database", "role", "stale"])
def test_image_failed_replacement_preserves_old(approved, failure):
    db, key = approved
    store = StoreFake(db)
    images.replace_picture(
        db, 1, key, db.get(CreatorApplication, key).version, png(), store
    )
    first = db.get(CreatorProfile, key).image_id
    original_key = db.get(ProfileImage, first).object_key
    actor, policy = (
        (2, {"administrative": True, "reason": "correction"})
        if failure == "role"
        else (1, {})
    )

    def fail_audit(*args):
        raise RuntimeError("injected persistence failure")

    if failure == "storage":
        store.fail_put = True
    elif failure == "database":
        sql_event.listen(BusinessAudit, "before_insert", fail_audit)
    else:

        def hook():
            with db.begin():
                if failure == "role":
                    db.delete(
                        db.scalar(
                            select(LocalRoleAssignment).where(
                                LocalRoleAssignment.role == "ADMIN"
                            )
                        )
                    )
                else:
                    db.get(CreatorApplication, key).version += 1

        store.hook = hook
    try:
        with pytest.raises((HTTPException, RuntimeError)):
            images.replace_picture(
                db,
                actor,
                key,
                db.get(CreatorApplication, key).version,
                png(),
                store,
                **policy,
            )
    finally:
        if failure == "database":
            sql_event.remove(BusinessAudit, "before_insert", fail_audit)
    db.expire_all()
    assert db.get(CreatorProfile, key).image_id == first
    assert list(store.objects) == [original_key]


def test_image_cleanup_expired_event_and_staged_upload(approved):
    db, key = approved
    store = StoreFake(db)
    images.replace_picture(
        db, 1, key, db.get(CreatorApplication, key).version, png(), store
    )
    db.add(
        ProfileImage(
            event_id=1,
            object_key="profiles/orphan.png",
            state="STAGED",
            delete_after=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    store.objects["profiles/orphan.png"] = b"orphan"
    db.get(Event, 1).data_delete_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert images.cleanup_images(db, store) == 0
    assert not store.objects
    assert db.get(CreatorProfile, key).image_id is None
    assert db.scalar(select(func.count()).select_from(ProfileImage)) == 0


def test_invitation_digest_expiry_resend_revoke_and_replay(approved):
    db, key = approved
    invitation_id, secret = invite(db, key)
    invitation = db.get(HelperInvitation, invitation_id)
    assert invitation.token_digest == hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in str(invitation.__dict__)
    assert workflow.utc(invitation.expires_at) <= workflow.utc(
        db.get(Event, 1).badge_change_deadline_at
    )
    new_id, new_secret = invite(db, key, replace_id=invitation_id)
    assert db.get(HelperInvitation, invitation_id).revoked_at
    with pytest.raises(HTTPException):
        run(helpers.redeem(db, 3, secret, RegistrationFake(), IdentityFake()))
    helper_id = run(
        helpers.redeem(db, 3, new_secret, RegistrationFake(), IdentityFake())
    )
    assert db.get(HelperRegistration, helper_id).status == "PENDING"
    assert db.get(HelperInvitation, new_id).consumed_by == 3
    with pytest.raises(HTTPException):
        run(helpers.redeem(db, 2, new_secret, RegistrationFake(), IdentityFake()))
    last_id, last_secret = invite(db, key)
    helpers.revoke(db, 1, last_id)
    with pytest.raises(HTTPException):
        run(helpers.redeem(db, 2, last_secret, RegistrationFake(), IdentityFake()))
    assert secret not in str(list(db.scalars(select(BusinessAudit.changes))))


def test_invitation_seven_day_default_and_expired(approved):
    db, key = approved
    db.get(Event, 1).badge_change_deadline_at = datetime.now(UTC) + timedelta(days=20)
    db.commit()
    invitation_id, secret = invite(db, key)
    row = db.get(HelperInvitation, invitation_id)
    assert workflow.utc(row.expires_at) - workflow.utc(row.created_at) == pytest.approx(
        timedelta(days=7), abs=timedelta(seconds=2)
    )
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    with pytest.raises(HTTPException):
        run(helpers.redeem(db, 3, secret, RegistrationFake(), IdentityFake()))


@pytest.mark.parametrize(
    "mode,code",
    [
        ("self", 409),
        ("invalid", 409),
        ("unavailable", 503),
        ("ineligible", 409),
        ("unknown", 503),
    ],
)
def test_redemption_fail_closed(approved, mode, code):
    db, key = approved
    invitation_id, secret = invite(db, key)
    fake = RegistrationFake()
    if mode == "unavailable":
        fake.failure = RegistrationUnavailable()
    if mode == "ineligible":
        fake.status = RegistrationStatus.INELIGIBLE
    if mode == "unknown":
        fake.status = RegistrationStatus.UNKNOWN
    with pytest.raises(HTTPException) as error:
        run(
            helpers.redeem(
                db,
                1 if mode == "self" else 3,
                "x" * 43 if mode == "invalid" else secret,
                fake,
                IdentityFake(),
            )
        )
    assert error.value.status_code == code
    assert db.get(HelperInvitation, invitation_id).consumed_at is None
    assert db.scalar(select(func.count()).select_from(HelperRegistration)) == 0


def test_redemption_rate_limit(approved, monkeypatch):
    db, _ = approved
    monkeypatch.setattr(settings, "invitation_attempts_per_minute", 2)
    for expected in (409, 409, 429):
        with pytest.raises(HTTPException) as error:
            run(helpers.redeem(db, 3, "x" * 43, RegistrationFake(), IdentityFake()))
        assert error.value.status_code == expected


def test_helper_limit_duplicates_and_decline(approved):
    db, key = approved
    db.get(Event, 1).helper_limit = 1
    db.commit()
    helper_id = register(db, key)
    _, secret = invite(db, key)
    with pytest.raises(HTTPException, match="limit"):
        run(helpers.redeem(db, 2, secret, RegistrationFake(), IdentityFake()))
    decide(db, helper_id, "DECLINED")
    second = run(helpers.redeem(db, 2, secret, RegistrationFake(), IdentityFake()))
    assert second != helper_id
    with pytest.raises(HTTPException, match="transition"):
        decide(db, helper_id)
    _, secret = invite(db, key)
    with pytest.raises(HTTPException, match="already have"):
        run(helpers.redeem(db, 3, secret, RegistrationFake(), IdentityFake()))


def test_multiple_creators_and_creator_can_help_elsewhere(approved):
    db, key = approved
    second_creator = approve(db, 2)
    first = register(db, key, user=3)
    second = register(db, second_creator, user=3, actor=2)
    third = register(db, second_creator, user=1, actor=2)
    assert len({first, second, third}) == 3
    assert db.scalar(select(func.count()).select_from(HelperRegistration)) == 3


def test_shared_sequence_reconfirmation_and_withdrawal(approved):
    db, key = approved
    helper_id = register(db, key)
    decide(db, helper_id)
    number = db.scalar(select(Badge.badge_number).where(Badge.helper_id == helper_id))
    assert db.scalar(select(Badge.badge_number).where(Badge.application_id == key)) == 1
    assert number == 2
    decide(db, helper_id, "DECLINED")
    decide(db, helper_id)
    withdraw(db, helper_id, actor=3, helper=True)
    with pytest.raises(HTTPException) as error:
        withdraw(db, helper_id, actor=3, helper=True, restore=True)
    assert error.value.status_code == 403
    withdraw(
        db,
        helper_id,
        actor=2,
        helper=True,
        restore=True,
        administrative=True,
        reason="Mistake",
    )
    assert (
        db.scalar(select(Badge.badge_number).where(Badge.helper_id == helper_id))
        == number
    )
    assert db.get(BadgeCounter, 1).next_number == 3
    assert helper_active(
        db.get(HelperRegistration, helper_id), db.get(CreatorApplication, key)
    )


@pytest.mark.parametrize("mode", ["withdraw", "approval_reversal"])
def test_creator_inactivity_preserves_helpers_and_badges(approved, mode):
    db, key = approved
    helper_id = register(db, key)
    decide(db, helper_id)
    unused_id, _ = invite(db, key)
    if mode == "withdraw":
        withdraw(db, key)
    else:
        record = db.get(CreatorApplication, key)
        run(
            workflow.review(
                db,
                2,
                key,
                record.version,
                "ON_REVIEW",
                "Mistaken approval",
                "",
                False,
                RegistrationFake(),
                IdentityFake(),
            )
        )
    helper = db.get(HelperRegistration, helper_id)
    assert helper.status == "CONFIRMED"
    assert not helper_active(helper, db.get(CreatorApplication, key))
    assert db.get(HelperInvitation, unused_id).revoked_at
    assert db.scalar(select(func.count()).select_from(Badge)) == 2
    if mode == "withdraw":
        withdraw(db, key, actor=2, restore=True, administrative=True, reason="Mistake")
    else:
        record = db.get(CreatorApplication, key)
        run(
            workflow.review(
                db,
                2,
                key,
                record.version,
                "APPROVED",
                "",
                "",
                False,
                RegistrationFake(),
                IdentityFake(),
            )
        )
    assert helper_active(helper, db.get(CreatorApplication, key))
    assert helper.status == "CONFIRMED" and db.get(BadgeCounter, 1).next_number == 3


@pytest.mark.parametrize("target", ["confirmation", "restoration"])
def test_helper_eligibility_recheck_does_not_change_state(approved, target):
    db, key = approved
    helper_id = register(db, key)
    if target == "restoration":
        decide(db, helper_id)
        withdraw(db, helper_id, actor=3, helper=True)
    record = db.get(HelperRegistration, helper_id)
    version, status = record.version, record.status
    fake = RegistrationFake()
    fake.status = RegistrationStatus.UNKNOWN
    operation = (
        helpers.decide(db, 1, helper_id, version, "CONFIRMED", fake, IdentityFake())
        if target == "confirmation"
        else helpers.withdraw(
            db,
            2,
            helper_id,
            version,
            fake,
            IdentityFake(),
            helper=True,
            restore=True,
            administrative=True,
            reason="Correction",
        )
    )
    with pytest.raises(HTTPException) as error:
        run(operation)
    assert error.value.status_code == 503
    assert db.get(HelperRegistration, helper_id).status == status
    assert db.get(HelperRegistration, helper_id).version == version


@pytest.mark.parametrize(
    "action",
    ["profile", "image", "invite", "confirm", "creator_withdraw", "helper_withdraw"],
)
def test_deadline_and_admin_exception(approved, action):
    db, key = approved
    helper_id = register(db, key)
    db.get(Event, 1).badge_change_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()

    def operation(admin=False):
        actor = 2 if admin else (3 if action == "helper_withdraw" else 1)
        policy = {
            "administrative": admin,
            "exceptional": admin,
            "reason": "Late correction" if admin else "",
        }
        version = db.get(CreatorApplication, key).version
        if action == "profile":
            return service.save_profile(
                db, actor, key, version, "new name", input_data().channels, **policy
            )
        if action == "image":
            return images.replace_picture(
                db, actor, key, version, png(), StoreFake(db), **policy
            )
        if action == "invite":
            return invite(db, key, actor=actor, **policy)
        if action == "confirm":
            return decide(db, helper_id, actor=actor, **policy)
        return withdraw(
            db,
            helper_id if action == "helper_withdraw" else key,
            actor=actor,
            helper=action == "helper_withdraw",
            **policy,
        )

    with pytest.raises(HTTPException) as error:
        operation()
    assert error.value.status_code == 409
    if action != "invite":
        operation(True)
        assert any(
            row.changes.get("exceptional") for row in db.scalars(select(BusinessAudit))
        )


def test_exact_deadline_boundary(approved):
    db, _ = approved
    event = db.get(Event, 1)
    cutoff = workflow.utc(event.badge_change_deadline_at)
    change_allowed(event, now=cutoff - timedelta(microseconds=1))
    for now in (cutoff, cutoff + timedelta(microseconds=1)):
        with pytest.raises(HTTPException):
            change_allowed(event, now=now)


def test_confirmation_transaction_rollback(approved):
    db, key = approved
    helper_id = register(db, key)

    def fail(*args):
        raise RuntimeError("outbox persistence unavailable")

    sql_event.listen(NotificationOutbox, "before_insert", fail)
    try:
        with pytest.raises(RuntimeError):
            decide(db, helper_id)
    finally:
        sql_event.remove(NotificationOutbox, "before_insert", fail)
    assert db.get(HelperRegistration, helper_id).status == "PENDING"
    assert db.scalar(select(func.count()).select_from(Badge)) == 1
    assert db.get(BadgeCounter, 1).next_number == 2
    assert not db.scalar(
        select(BusinessAudit.id).where(BusinessAudit.action == "helper_decided")
    )


def test_creator_and_helper_cross_access(approved):
    db, key = approved
    helper_id = register(db, key)
    invitation_id, _ = invite(db, key)
    for operation in (
        lambda: decide(db, helper_id, actor=3),
        lambda: withdraw(db, helper_id, actor=1, helper=True),
        lambda: helpers.revoke(db, 3, invitation_id),
        lambda: invite(db, key, actor=3),
    ):
        with pytest.raises(HTTPException) as error:
            operation()
        assert error.value.status_code == 404
        db.rollback()


def test_m3_browser_flow_privacy_csrf_and_upload(browser, monkeypatch):
    monkeypatch.setattr(settings, "s3_bucket", None)
    client, _, engine = browser
    with Session(engine) as db:
        key = approve(db)
    token = csrf(client, f"/creators/{key}")
    response = client.post(
        f"/creators/{key}/profile",
        data={
            "csrf_token": token,
            "version": 3,
            "channel_name": "Public name",
            "platform": "Twitch",
            "account": "@creator",
            "primary": "0",
        },
    )
    assert response.status_code == 200
    token = csrf(client, f"/creators/{key}")
    # Filename and MIME are deliberately wrong: decoded image is authoritative.
    response = client.post(
        f"/creators/{key}/picture",
        data={"csrf_token": token, "version": 4},
        files={"picture": ("hostile.exe", png(), "application/octet-stream")},
    )
    assert response.status_code == 503  # Missing real storage is explicit.
    assert client.post(f"/creators/{key}/profile", data={}).status_code == 403
    with Session(engine) as db:
        helper_id = register(db, key)
    response = client.get(f"/creators/{key}")
    assert "Test attendee" in response.text
    assert (
        "attendee@example.test" not in response.text and "REG-123" not in response.text
    )
    login(client, 3)
    assert client.get(f"/creators/{key}").status_code == 404
    token = csrf(client, "/helpers")
    response = client.post(
        f"/helpers/{helper_id}/decision",
        data={"csrf_token": token, "version": 1, "status": "CONFIRMED"},
    )
    assert response.status_code == 404
    response = client.post(
        f"/creators/{key}/picture",
        data={"csrf_token": token, "version": 4},
        files={"picture": ("x.png", png(), "image/png")},
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "constraint", ["self", "badge_owner", "duplicate", "negative_limit"]
)
def test_m3_database_constraints(approved, constraint):
    db, key = approved
    helper_id = register(db, key)
    decide(db, helper_id)
    db.rollback()
    with pytest.raises(IntegrityError), db.begin():
        if constraint == "self":
            db.execute(
                update(HelperRegistration)
                .where(HelperRegistration.id == helper_id)
                .values(user_id=1)
            )
        elif constraint == "badge_owner":
            db.add(
                Badge(
                    event_id=1, application_id=key, helper_id=helper_id, badge_number=99
                )
            )
        elif constraint == "negative_limit":
            db.get(Event, 1).helper_limit = -1
        else:
            row = db.get(HelperRegistration, helper_id)
            db.add(
                HelperRegistration(
                    application_id=key,
                    event_id=1,
                    user_id=3,
                    creator_user_id=1,
                    invitation_id=row.invitation_id,
                    eligibility_checked_at=datetime.now(UTC),
                )
            )
        db.flush()


def test_postgres_concurrent_redemption_and_allocations(application_engine):
    if application_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL locking verification")
    with Session(application_engine) as db:
        key = approve(db)
        _, secret = invite(db, key)
    barrier = Barrier(2)

    class SynchronizedRegistration(RegistrationFake):
        async def lookup(self, lookup):
            barrier.wait(timeout=10)
            return await super().lookup(lookup)

    def redeem_as(user_id):
        with Session(application_engine) as db:
            try:
                return run(
                    helpers.redeem(
                        db, user_id, secret, SynchronizedRegistration(), IdentityFake()
                    )
                )
            except HTTPException as error:
                return error.status_code

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(redeem_as, (2, 3)))
    assert results.count(409) == 1
    with Session(application_engine) as db:
        first = db.scalar(select(HelperRegistration))
        second = register(db, key, user=3 if first.user_id == 2 else 2)
        ids = [first.id, second]
    barrier.reset()

    def confirm(helper_id):
        with Session(application_engine) as db:
            run(
                helpers.decide(
                    db,
                    1,
                    helper_id,
                    1,
                    "CONFIRMED",
                    SynchronizedRegistration(),
                    IdentityFake(),
                )
            )

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(confirm, ids))
    with Session(application_engine) as db:
        assert list(
            db.scalars(select(Badge.badge_number).order_by(Badge.badge_number))
        ) == [1, 2, 3]


def test_invitation_browser_redemption_and_admin_restoration(browser):
    client, _, engine = browser
    with Session(engine) as db:
        key = approve(db)
    token = csrf(client, f"/creators/{key}")
    response = client.post(f"/creators/{key}/invitations", data={"csrf_token": token})
    assert response.status_code == 200
    import re

    secret = re.search(r"/helpers/redeem#([A-Za-z0-9_-]+)", response.text)[1]
    assert response.headers["cache-control"] == "no-store"
    client.cookies.clear()
    assert "Sign in to register" in client.get("/helpers/redeem").text
    assert (
        client.post("/helpers/redeem", data={"invitation": secret}).status_code == 401
    )
    login(client, 3)
    token = csrf(client, "/helpers/redeem")
    response = client.post(
        "/helpers/redeem", data={"csrf_token": token, "invitation": secret}
    )
    assert response.status_code == 200 and "PENDING" in response.text
    with Session(engine) as db:
        helper = db.scalar(select(HelperRegistration))
        helper_id = helper.id
    login(client, 1)
    token = csrf(client, f"/creators/{key}")
    assert (
        client.post(
            f"/helpers/{helper_id}/decision",
            data={"csrf_token": token, "version": 1, "status": "CONFIRMED"},
        ).status_code
        == 200
    )
    login(client, 3)
    token = csrf(client, "/helpers")
    assert (
        client.post(
            f"/participation/helper/{helper_id}",
            data={"csrf_token": token, "version": 2, "action": "withdraw"},
        ).status_code
        == 200
    )
    login(client, 2)
    token = csrf(client, f"/creators/{key}?administrative=true")
    assert (
        client.post(
            f"/participation/helper/{helper_id}",
            data={
                "csrf_token": token,
                "version": 3,
                "action": "restore",
                "administrative": "yes",
                "reason": "Mistaken withdrawal",
            },
        ).status_code
        == 200
    )
    with Session(engine) as db:
        helper = db.get(HelperRegistration, helper_id)
        assert helper.status == "CONFIRMED" and helper.withdrawn_at is None


def test_successful_upload_route_and_private_image(browser, monkeypatch):
    from app.main import app

    client, _, engine = browser
    objects = {}

    class BrowserStore:
        def put(self, key, data):
            objects[key] = data

        def get(self, key):
            return objects[key]

        def delete(self, key):
            objects.pop(key, None)

    app.dependency_overrides[images.get_image_store] = BrowserStore
    with Session(engine) as db:
        key = approve(db)
    token = csrf(client, f"/creators/{key}")
    response = client.post(
        f"/creators/{key}/picture",
        data={"csrf_token": token, "version": 3},
        files={"picture": ("../../private.exe", png(), "text/plain")},
    )
    assert response.status_code == 200
    response = client.get(f"/creators/{key}/picture")
    assert (
        response.status_code == 200 and response.headers["content-type"] == "image/png"
    )
    assert response.headers["cache-control"] == "no-store"
    assert len(objects) == 1 and "private" not in next(iter(objects))
    with Image.open(BytesIO(response.content)) as image:
        assert image.info["dpi"] == pytest.approx((300, 300), abs=0.01)
    login(client, 3)
    assert client.get(f"/creators/{key}/picture").status_code == 403
    login(client, 2)
    assert client.get(f"/creators/{key}/picture").status_code == 200
    with Session(engine) as db:
        role = db.scalar(
            select(LocalRoleAssignment).where(LocalRoleAssignment.role == "ADMIN")
        )
        db.delete(role)
        db.commit()
    assert client.get(f"/creators/{key}/picture").status_code == 403
    login(client, 1)
    token = csrf(client, f"/creators/{key}")
    monkeypatch.setattr(settings, "profile_image_max_bytes", 128)
    assert (
        client.post(
            f"/creators/{key}/picture",
            data={"csrf_token": token, "version": 4},
            files={"picture": ("large.png", b"x" * 70000)},
        ).status_code
        == 413
    )
    assert len(objects) == 1


def test_external_checks_outside_transaction_and_context_recheck(approved):
    db, key = approved
    _, secret = invite(db, key)
    fake = RegistrationFake()

    def hook():
        assert not db.in_transaction()
        with db.begin():
            db.get(CreatorApplication, key).withdrawn_at = datetime.now(UTC)

    fake.hook = hook
    with pytest.raises(HTTPException, match="active approved"):
        run(helpers.redeem(db, 3, secret, fake, IdentityFake()))
    assert db.scalar(select(func.count()).select_from(HelperRegistration)) == 0


def test_withdrawn_helper_does_not_consume_capacity(approved):
    db, key = approved
    db.get(Event, 1).helper_limit = 1
    db.commit()
    first = register(db, key)
    withdraw(db, first, actor=3, helper=True)
    second = register(db, key, user=2)
    assert first != second
    with pytest.raises(HTTPException, match="limit"):
        withdraw(
            db,
            first,
            actor=2,
            helper=True,
            restore=True,
            administrative=True,
            reason="Mistake",
        )


def test_wrong_event_and_independent_number_sequence(approved, monkeypatch):
    db, key = approved
    helper_id = register(db, key)
    _, secret = invite(db, key)
    previous = db.get(Event, 1)
    db.add(
        Event(
            id=2,
            year=2029,
            name="Other",
            starts_at=previous.starts_at,
            ends_at=previous.ends_at,
            application_open_at=previous.application_open_at,
            application_close_at=previous.application_close_at,
            badge_print_at=previous.badge_print_at,
            badge_change_deadline_at=previous.badge_change_deadline_at,
            data_delete_at=previous.data_delete_at,
        )
    )
    db.commit()
    monkeypatch.setattr(settings, "active_event_id", 2)
    for operation in (
        lambda: decide(db, helper_id),
        lambda: invite(db, key),
        lambda: run(helpers.redeem(db, 2, secret, RegistrationFake(), IdentityFake())),
    ):
        with pytest.raises(HTTPException):
            operation()
        db.rollback()
    second = approve(db)
    assert (
        db.scalar(select(Badge.badge_number).where(Badge.application_id == second)) == 1
    )


def test_stale_confirmation_and_resend_do_not_duplicate(approved):
    db, key = approved
    helper_id = register(db, key)
    decide(db, helper_id)
    with pytest.raises(HTTPException, match="changed"):
        run(
            helpers.decide(
                db, 1, helper_id, 1, "CONFIRMED", RegistrationFake(), IdentityFake()
            )
        )
    old_id, _ = invite(db, key)
    invite(db, key, replace_id=old_id)
    with pytest.raises(HTTPException):
        invite(db, key, replace_id=old_id)
    assert db.scalar(select(func.count()).select_from(Badge)) == 2


def test_missing_deadline_and_admin_authorization(approved):
    db, key = approved
    version = db.get(CreatorApplication, key).version
    with pytest.raises(HTTPException) as error:
        service.save_profile(
            db,
            3,
            key,
            version,
            "name",
            input_data().channels,
            administrative=True,
            exceptional=True,
            reason="Not authorized",
        )
    assert error.value.status_code == 403
    db.get(Event, 1).badge_change_deadline_at = None
    db.commit()
    with pytest.raises(HTTPException) as error:
        invite(db, key)
    assert error.value.status_code == 503


def test_postgres_concurrent_limit_and_same_helper_decision(application_engine):
    if application_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL locking verification")
    with Session(application_engine) as db:
        key = approve(db)
        db.get(Event, 1).helper_limit = 1
        db.commit()
        secrets = [invite(db, key)[1], invite(db, key)[1]]
    barrier = Barrier(2)

    class SynchronizedRegistration(RegistrationFake):
        async def lookup(self, lookup):
            barrier.wait(timeout=10)
            return await super().lookup(lookup)

    def redeem(index):
        with Session(application_engine) as db:
            try:
                run(
                    helpers.redeem(
                        db,
                        index + 2,
                        secrets[index],
                        SynchronizedRegistration(),
                        IdentityFake(),
                    )
                )
                return 200
            except HTTPException as error:
                return error.status_code

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(redeem, (0, 1))) == [200, 409]
    with Session(application_engine) as db:
        helper_id = db.scalar(select(HelperRegistration.id))
    barrier.reset()

    def confirm(_):
        with Session(application_engine) as db:
            try:
                run(
                    helpers.decide(
                        db,
                        1,
                        helper_id,
                        1,
                        "CONFIRMED",
                        SynchronizedRegistration(),
                        IdentityFake(),
                    )
                )
                return 200
            except HTTPException as error:
                return error.status_code

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(confirm, (0, 1))) == [200, 409]
    with Session(application_engine) as db:
        assert db.scalar(select(func.count()).select_from(Badge)) == 2
        assert (
            db.scalar(
                select(func.count())
                .select_from(NotificationOutbox)
                .where(NotificationOutbox.notification_type == "HELPER_CONFIRMED")
            )
            == 1
        )
