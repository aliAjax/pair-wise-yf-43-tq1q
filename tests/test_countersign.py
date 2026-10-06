import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _future(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")


def _past(days=1):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


class CountersignFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin-1", "admin")
        self.analyst = Actor("analyst-1", "analyst")
        self.authorizer = Actor("authorizer-1", "authorizer")
        self.instrument = self._instrument()
        self.method = self._method()

    def tearDown(self):
        self.tmp.cleanup()

    def _instrument(self):
        instrument = self.service.create(
            self.admin, "instrument", {"name": "Analyzer", "serial": "A-1"}
        )
        self.service.transition(
            self.admin, instrument["id"], "send_calibration", {}
        )
        self.service.transition(
            self.admin,
            instrument["id"],
            "calibrate",
            {"due_at": "2099-01-01", "passed": True},
        )
        return self.service.get(instrument["id"])

    def _method(self):
        method = self.service.create(
            self.admin, "method", {"name": "Assay", "version": "v1"}
        )
        self.service.transition(
            self.admin,
            method["id"],
            "validate_method",
            {"parameters": {"range": [0, 10]}, "instrument_ids": [self.instrument["id"]]},
        )
        return self.service.get(method["id"])

    def create_result(self, sample="S-1"):
        return self.service.create(
            self.analyst,
            "result",
            {"sample_id": sample, "measurement": "m"},
        )

    def sign_analyst(self, result_id, actor=None, expected_version=None, **extra):
        data = {
            "instrument_id": self.instrument["id"],
            "method_id": self.method["id"],
            "value": 4.2,
            "unit": "mg/L",
        }
        data.update(extra)
        return self.service.transition(
            actor or self.analyst,
            result_id,
            "sign",
            data,
            expected_version=expected_version,
        )

    def sign_authorizer(self, result_id, actor=None, expected_version=None):
        return self.service.transition(
            actor or self.authorizer,
            result_id,
            "sign",
            {},
            expected_version=expected_version,
        )

    def delegate(self, principal, agent, role, expires_at=None):
        data = {"principal_id": principal, "agent_id": agent, "role": role}
        if expires_at:
            data["expires_at"] = expires_at
        return self.service.create(self.admin, "delegation", data)


class CountersignTest(CountersignFixture):
    def test_two_distinct_people_release_result(self):
        result = self.create_result()
        first = self.sign_analyst(result["id"])
        self.assertEqual(first["status"], "countersigning")
        second = self.sign_authorizer(result["id"])
        self.assertEqual(second["status"], "released")
        self.assertEqual(second["version"], 3)
        signatures = second["data"]["signatures"]
        self.assertEqual(signatures["analyst"]["signed_by"], "analyst-1")
        self.assertEqual(signatures["authorizer"]["signed_by"], "authorizer-1")

    def test_authorizer_cannot_sign_before_analyst(self):
        result = self.create_result()
        with self.assertRaises(InvalidTransition):
            self.sign_authorizer(result["id"])

    def test_same_person_cannot_hold_both_signatures(self):
        result = self.create_result()
        self.sign_analyst(result["id"])
        # An analyst-account that happens to also carry authorizer role is
        # still the same physical person and must be refused.
        dual = Actor("analyst-1", "authorizer")
        with self.assertRaises(PermissionDenied):
            self.sign_authorizer(result["id"], actor=dual)

    def test_analyst_cannot_sign_twice(self):
        result = self.create_result()
        self.sign_analyst(result["id"])
        with self.assertRaises(InvalidTransition):
            self.sign_analyst(result["id"])

    def test_second_signature_cannot_replace_release_package(self):
        result = self.create_result()
        self.sign_analyst(result["id"], value=4.2)
        released = self.sign_authorizer(result["id"])
        self.assertEqual(released["data"]["value"], 4.2)
        self.assertEqual(released["data"]["unit"], "mg/L")

    def test_first_sign_still_checks_instrument_and_method(self):
        bad = self.service.create(
            self.analyst, "result", {"sample_id": "S-2", "measurement": "m"}
        )
        with self.assertRaises(ValidationError):
            self.sign_analyst(
                bad["id"], instrument_id="missing", method_id=self.method["id"]
            )


class DelegationTest(CountersignFixture):
    def test_delegated_sign_recorded_under_principal_but_audited_as_agent(self):
        delegation = self.delegate("authorizer-1", "backup-1", "authorizer")
        result = self.create_result()
        self.sign_analyst(result["id"])
        backup = Actor("backup-1", "authorizer", on_behalf_of="authorizer-1")
        released = self.sign_authorizer(result["id"], actor=backup)
        slot = released["data"]["signatures"]["authorizer"]
        # 代做的操作记在委托人名下
        self.assertEqual(slot["signed_by"], "authorizer-1")
        self.assertEqual(slot["physical_actor"], "backup-1")
        self.assertEqual(slot["delegation_id"], delegation["id"])

        timeline = self.service.audit_log(result["id"])
        sign_event = next(event for event in timeline if event["action"] == "sign"
                          and event["to_status"] == "released")
        self.assertEqual(sign_event["actor_id"], "backup-1")
        self.assertEqual(sign_event["detail"]["delegated"]["principal_id"], "authorizer-1")

    def test_delegated_analyst_sign(self):
        self.delegate("analyst-1", "backup-1", "analyst")
        result = self.create_result()
        backup = Actor("backup-1", "analyst", on_behalf_of="analyst-1")
        countersigning = self.sign_analyst(result["id"], actor=backup)
        slot = countersigning["data"]["signatures"]["analyst"]
        self.assertEqual(slot["signed_by"], "analyst-1")
        self.assertEqual(slot["physical_actor"], "backup-1")

    def test_overreaching_agent_is_refused(self):
        # Only an analyst post is delegated; signing authorizer is 越权代签.
        self.delegate("analyst-1", "backup-1", "analyst")
        result = self.create_result()
        self.sign_analyst(result["id"])
        backup = Actor("backup-1", "analyst", on_behalf_of="analyst-1")
        with self.assertRaises(PermissionDenied):
            self.sign_authorizer(result["id"], actor=backup)

    def test_no_delegation_header_refused_when_not_holding_role(self):
        result = self.create_result()
        self.sign_analyst(result["id"])
        stranger = Actor("stranger-1", "viewer", on_behalf_of="authorizer-1")
        with self.assertRaises(PermissionDenied):
            self.sign_authorizer(result["id"], actor=stranger)

    def test_nonexistent_delegation_is_refused(self):
        result = self.create_result()
        self.sign_analyst(result["id"])
        fake = Actor("backup-1", "authorizer", on_behalf_of="authorizer-1")
        with self.assertRaises(PermissionDenied):
            self.sign_authorizer(result["id"], actor=fake)

    def test_delegating_to_someone_else_not_yourself(self):
        with self.assertRaises(ValidationError):
            self.delegate("analyst-1", "analyst-1", "analyst")

    def test_only_admin_creates_delegations(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(
                self.analyst,
                "delegation",
                {"principal_id": "authorizer-1", "agent_id": "x", "role": "authorizer"},
            )

    def test_post_change_invalidates_previous_delegation(self):
        first = self.delegate("authorizer-1", "backup-1", "authorizer")
        second = self.delegate("authorizer-1", "backup-2", "authorizer")
        self.assertEqual(self.service.get(first["id"])["status"], "superseded")
        self.assertEqual(self.service.get(second["id"])["status"], "active")

        result = self.create_result()
        self.sign_analyst(result["id"])
        old_agent = Actor("backup-1", "authorizer", on_behalf_of="authorizer-1")
        with self.assertRaises(PermissionDenied):
            self.sign_authorizer(result["id"], actor=old_agent)
        new_agent = Actor("backup-2", "authorizer", on_behalf_of="authorizer-1")
        self.assertEqual(self.sign_authorizer(result["id"], actor=new_agent)["status"], "released")

    def test_revoked_delegation_is_invalid(self):
        delegation = self.delegate("authorizer-1", "backup-1", "authorizer")
        self.service.transition(self.admin, delegation["id"], "revoke", {})
        result = self.create_result()
        self.sign_analyst(result["id"])
        backup = Actor("backup-1", "authorizer", on_behalf_of="authorizer-1")
        with self.assertRaises(PermissionDenied):
            self.sign_authorizer(result["id"], actor=backup)
        self.assertEqual(self.service.get(delegation["id"])["status"], "revoked")

    def test_expired_delegation_is_invalid(self):
        delegation = self.delegate("authorizer-1", "backup-1", "authorizer")
        self.repo.update_entity(
            delegation["id"],
            delegation["version"],
            "active",
            dict(delegation["data"], expires_at=_past()),
        )
        result = self.create_result()
        self.sign_analyst(result["id"])
        backup = Actor("backup-1", "authorizer", on_behalf_of="authorizer-1")
        with self.assertRaises(PermissionDenied):
            self.sign_authorizer(result["id"], actor=backup)

    def test_valid_until_expiry_still_works(self):
        self.delegate("authorizer-1", "backup-1", "authorizer", expires_at=_future(2))
        result = self.create_result()
        self.sign_analyst(result["id"])
        backup = Actor("backup-1", "authorizer", on_behalf_of="authorizer-1")
        self.assertEqual(self.sign_authorizer(result["id"], actor=backup)["status"], "released")

    def test_principal_with_active_delegation_still_signs_directly(self):
        self.delegate("authorizer-1", "backup-1", "authorizer")
        result = self.create_result()
        self.sign_analyst(result["id"])
        self.assertEqual(self.sign_authorizer(result["id"])["status"], "released")

    def test_delegated_agent_cannot_perform_unrelated_action(self):
        # An analyst delegation must not allow, e.g., method validation.
        self.delegate("analyst-1", "backup-1", "analyst")
        method = self.service.create(
            self.admin, "method", {"name": "M2", "version": "v2"}
        )
        backup = Actor("backup-1", "analyst", on_behalf_of="analyst-1")
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                backup,
                method["id"],
                "validate_method",
                {"parameters": {"range": [0, 1]}, "instrument_ids": [self.instrument["id"]]},
            )

    def test_agent_holding_two_posts_still_cannot_sign_both_slots(self):
        # Even if the same person somehow receives two post delegations, the
        # physical-person check blocks them from holding both signatures.
        self.delegate("analyst-1", "backup-1", "analyst")
        self.delegate("authorizer-1", "backup-1", "authorizer")
        result = self.create_result()
        backup_analyst = Actor("backup-1", "analyst", on_behalf_of="analyst-1")
        self.sign_analyst(result["id"], actor=backup_analyst)
        backup_auth = Actor("backup-1", "authorizer", on_behalf_of="authorizer-1")
        with self.assertRaises(PermissionDenied):
            self.sign_authorizer(result["id"], actor=backup_auth)

    def test_delegation_hint_selects_between_two_active_delegations(self):
        # Same principal delegates the same post to two people is impossible
        # (the older one is superseded), but the client may pin a specific
        # delegation via X-Delegation-Id; a wrong hint is refused.
        delegation = self.delegate("authorizer-1", "backup-1", "authorizer")
        result = self.create_result()
        self.sign_analyst(result["id"])
        hinted = Actor(
            "backup-1", "authorizer",
            on_behalf_of="authorizer-1", delegation_id="does-not-exist",
        )
        with self.assertRaises(PermissionDenied):
            self.sign_authorizer(result["id"], actor=hinted)
        good = Actor(
            "backup-1", "authorizer",
            on_behalf_of="authorizer-1", delegation_id=delegation["id"],
        )
        self.assertEqual(
            self.sign_authorizer(result["id"], actor=good)["status"], "released"
        )

    def test_agent_id_header_must_match_delegation(self):
        self.delegate("authorizer-1", "backup-1", "authorizer")
        result = self.create_result()
        self.sign_analyst(result["id"])
        wrong = Actor("someone-else", "authorizer", on_behalf_of="authorizer-1")
        with self.assertRaises(PermissionDenied):
            self.sign_authorizer(result["id"], actor=wrong)


class WithdrawTest(CountersignFixture):
    def test_withdraw_second_signature_keeps_first(self):
        result = self.create_result()
        self.sign_analyst(result["id"])
        countersigning_version = self.sign_authorizer(result["id"])
        self.assertEqual(countersigning_version["status"], "released")
        back = self.service.transition(
            self.authorizer, result["id"], "withdraw", {"slot": "authorizer"}
        )
        self.assertEqual(back["status"], "countersigning")
        self.assertIsNone(back["data"]["signatures"]["authorizer"])
        self.assertEqual(
            back["data"]["signatures"]["analyst"]["signed_by"], "analyst-1"
        )

    def test_withdraw_first_signature_when_both_are_present_keeps_second(self):
        result = self.create_result()
        self.sign_analyst(result["id"])
        self.sign_authorizer(result["id"])
        back = self.service.transition(
            self.analyst, result["id"], "withdraw", {"slot": "analyst"}
        )
        self.assertEqual(back["status"], "countersigning")
        self.assertIsNone(back["data"]["signatures"]["analyst"])
        self.assertEqual(
            back["data"]["signatures"]["authorizer"]["signed_by"], "authorizer-1"
        )
        # analyst-first ordering applies again before release can complete
        self.sign_analyst(result["id"])
        self.assertEqual(self.service.get(result["id"])["status"], "released")

    def test_withdraw_only_signature_returns_to_pending(self):
        result = self.create_result()
        self.sign_analyst(result["id"])
        back = self.service.transition(
            self.analyst, result["id"], "withdraw", {"slot": "analyst"}
        )
        self.assertEqual(back["status"], "pending")
        self.assertIsNone(back["data"]["signatures"]["analyst"])

    def test_stranger_cannot_withdraw_signature(self):
        result = self.create_result()
        self.sign_analyst(result["id"])
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.authorizer, result["id"], "withdraw", {"slot": "analyst"}
            )

    def test_withdraw_missing_slot_is_invalid(self):
        result = self.create_result()
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.analyst, result["id"], "withdraw", {"slot": "analyst"}
            )

    def test_withdraw_first_then_other_withdraws_too_returns_to_pending(self):
        result = self.create_result()
        self.sign_analyst(result["id"])
        self.sign_authorizer(result["id"])
        self.service.transition(
            self.analyst, result["id"], "withdraw", {"slot": "analyst"}
        )
        back = self.service.transition(
            self.authorizer, result["id"], "withdraw", {"slot": "authorizer"}
        )
        self.assertEqual(back["status"], "pending")
        self.assertIsNone(back["data"]["signatures"]["analyst"])
        self.assertIsNone(back["data"]["signatures"]["authorizer"])
        # full fresh countersign flow works again and needs the release package
        self.sign_analyst(result["id"])
        self.sign_authorizer(result["id"])
        self.assertEqual(self.service.get(result["id"])["status"], "released")

    def test_agent_can_withdraw_signature_recorded_under_principal(self):
        self.delegate("authorizer-1", "backup-1", "authorizer")
        result = self.create_result()
        self.sign_analyst(result["id"])
        backup = Actor("backup-1", "authorizer", on_behalf_of="authorizer-1")
        self.sign_authorizer(result["id"], actor=backup)
        back = self.service.transition(
            backup, result["id"], "withdraw", {"slot": "authorizer"}
        )
        self.assertEqual(back["status"], "countersigning")
        self.assertIsNone(back["data"]["signatures"]["authorizer"])


class ConcurrentSecondSignTest(CountersignFixture):
    def test_second_signer_loses_race_gets_version_conflict(self):
        # Both signatures present; now simulate two people racing to place
        # what each believes is the missing second signature on another
        # result branch. Use the released re-sign path: reset to a fresh
        # countersigning result and have two authorizer-like actors submit
        # the second signature against the same base version.
        result = self.create_result()
        base = self.sign_analyst(result["id"])

        barrier = threading.Barrier(2)
        outcomes = []

        def attempt(actor):
            barrier.wait()
            try:
                self.sign_authorizer(
                    result["id"], actor=actor, expected_version=base["version"]
                )
                outcomes.append("ok:" + actor.user_id)
            except ConflictError:
                outcomes.append("conflict:" + actor.user_id)
            except Exception as exc:  # pragma: no cover - diagnostic
                outcomes.append(type(exc).__name__ + ":" + actor.user_id)

        t1 = threading.Thread(target=attempt, args=(self.authorizer,))
        t2 = threading.Thread(
            target=attempt, args=(Actor("authorizer-2", "authorizer"),)
        )
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(len(outcomes), 2)
        self.assertEqual(sorted(item.split(":")[0] for item in outcomes),
                         ["conflict", "ok"])
        final = self.service.get(result["id"])
        self.assertEqual(final["status"], "released")

    def test_late_second_signer_never_overwrites(self):
        result = self.create_result()
        base = self.sign_analyst(result["id"])
        first = self.sign_authorizer(
            result["id"], expected_version=base["version"]
        )
        self.assertEqual(first["status"], "released")
        # A stale expected version is reported as a version conflict even
        # before the state guard runs.
        with self.assertRaises(ConflictError):
            self.sign_authorizer(
                result["id"],
                actor=Actor("authorizer-2", "authorizer"),
                expected_version=base["version"],
            )
        with self.assertRaises(InvalidTransition):
            self.sign_authorizer(
                result["id"],
                actor=Actor("authorizer-2", "authorizer"),
                expected_version=first["version"],
            )


class AuditTimelineTest(CountersignFixture):
    def test_delegation_lifecycle_and_signatures_are_timed(self):
        first = self.delegate("authorizer-1", "backup-1", "authorizer")
        second = self.delegate("authorizer-1", "backup-2", "authorizer")
        delegation_log = self.service.audit_log(first["id"])
        actions = [event["action"] for event in delegation_log]
        self.assertEqual(actions, ["create", "supersede"])
        created_at = self.service.audit_log(second["id"])[0]["created_at"]
        self.assertTrue(created_at)

        result = self.create_result()
        self.sign_analyst(result["id"])
        backup = Actor("backup-2", "authorizer", on_behalf_of="authorizer-1")
        self.sign_authorizer(result["id"], actor=backup)
        self.service.transition(
            backup, result["id"], "withdraw", {"slot": "authorizer"}
        )
        log = self.service.audit_log(result["id"])
        names = [event["action"] for event in log]
        self.assertEqual(names, ["create", "sign", "sign", "withdraw"])
        # Timeline is strictly time-ordered (ids increasing).
        self.assertEqual([event["id"] for event in log],
                         sorted(event["id"] for event in log))
        withdrawn = log[-1]
        self.assertEqual(withdrawn["detail"]["withdrawn_slot"], "authorizer")
        self.assertEqual(withdrawn["detail"]["recorded_under"], "authorizer-1")
        # the delegated sign event keeps the physical actor
        delegated_sign = log[2]
        self.assertEqual(delegated_sign["actor_id"], "backup-2")
        self.assertEqual(
            delegated_sign["detail"]["delegated"]["delegation_id"], second["id"]
        )


if __name__ == "__main__":
    unittest.main()
