import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class CountersignTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.alice = Actor("alice", "analyst")
        self.carol = Actor("carol", "authorizer")
        self.bob = Actor("bob", "technician")

    def tearDown(self):
        self.tmp.cleanup()

    def _result(self):
        return self.service.create(
            self.admin, "result", {"sample_id": "S-1", "measurement": "initial"}
        )

    def _delegate(self, role, from_user, to_user, valid_from=None, valid_to=None):
        now = datetime.now(timezone.utc)
        valid_from = (valid_from or (now - timedelta(days=1))).isoformat()
        valid_to = (valid_to or (now + timedelta(days=1))).isoformat()
        return self.service.create(
            self.admin,
            "delegation",
            {
                "role": role,
                "from_user_id": from_user,
                "to_user_id": to_user,
                "valid_from": valid_from,
                "valid_to": valid_to,
                "reason": "leave coverage",
            },
        )

    def test_dual_signature_releases_result(self):
        result = self._result()
        first = self.service.transition(self.alice, result["id"], "sign_analyst", {})
        self.assertEqual(first["status"], "awaiting_authorizer")
        self.assertEqual(first["data"]["signatures"]["analyst"]["signer"], "alice")
        self.assertIsNone(first["data"]["signatures"]["analyst"]["on_behalf_of"])

        second = self.service.transition(self.carol, result["id"], "sign_authorizer", {})
        self.assertEqual(second["status"], "released")
        self.assertEqual(second["data"]["signatures"]["authorizer"]["signer"], "carol")

    def test_two_signatures_must_be_different_persons(self):
        # Bob is an analyst; Carol delegates her authorizer post to Bob.
        bob = Actor("bob", "analyst")
        self._delegate("authorizer", "carol", "bob")
        result = self._result()
        self.service.transition(bob, result["id"], "sign_analyst", {})
        with self.assertRaises(ValidationError):
            self.service.transition(bob, result["id"], "sign_authorizer", {})

    def test_delegation_allows_signing_and_records_delegator(self):
        self._delegate("analyst", "alice", "bob")
        result = self._result()
        first = self.service.transition(self.bob, result["id"], "sign_analyst", {})
        self.assertEqual(first["status"], "awaiting_authorizer")
        sig = first["data"]["signatures"]["analyst"]
        self.assertEqual(sig["signer"], "bob")
        self.assertEqual(sig["on_behalf_of"], "alice")

        second = self.service.transition(self.carol, result["id"], "sign_authorizer", {})
        self.assertEqual(second["status"], "released")

    def test_unauthorized_proxy_sign_is_rejected(self):
        result = self._result()
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.bob, result["id"], "sign_analyst", {})

    def test_delegation_is_invalidated_when_post_changes(self):
        first = self._delegate("analyst", "alice", "bob")
        self._delegate("analyst", "alice", "carol")

        self.assertEqual(self.service.get(first["id"])["status"], "revoked")
        result = self._result()
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.bob, result["id"], "sign_analyst", {})
        signed = self.service.transition(self.carol, result["id"], "sign_analyst", {})
        self.assertEqual(signed["status"], "awaiting_authorizer")

    def test_revoked_delegation_cannot_sign(self):
        delegation = self._delegate("analyst", "alice", "bob")
        self.service.transition(self.admin, delegation["id"], "revoke", {})
        result = self._result()
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.bob, result["id"], "sign_analyst", {})

    def test_expired_delegation_cannot_sign(self):
        now = datetime.now(timezone.utc)
        self._delegate(
            "analyst",
            "alice",
            "bob",
            valid_from=now - timedelta(days=10),
            valid_to=now - timedelta(days=1),
        )
        result = self._result()
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.bob, result["id"], "sign_analyst", {})

    def test_delegator_can_withdraw_proxy_signature(self):
        self._delegate("analyst", "alice", "bob")
        result = self._result()
        self.service.transition(self.bob, result["id"], "sign_analyst", {})
        self.service.transition(self.carol, result["id"], "sign_authorizer", {})
        # Alice (the delegator, whose name the signature is under) withdraws.
        withdrawn = self.service.transition(
            self.alice, result["id"], "withdraw_signature", {"sign_role": "analyst"}
        )
        self.assertEqual(withdrawn["status"], "awaiting_analyst")
        self.assertNotIn("analyst", withdrawn["data"]["signatures"])

    def test_withdraw_signature_returns_to_awaiting_and_retains_other(self):
        result = self._result()
        self.service.transition(self.alice, result["id"], "sign_analyst", {})
        self.service.transition(self.carol, result["id"], "sign_authorizer", {})
        withdrawn = self.service.transition(
            self.alice, result["id"], "withdraw_signature", {"sign_role": "analyst"}
        )
        # Withdrawing the analyst signature returns the result to awaiting the
        # analyst; the authorizer signature is retained.
        self.assertEqual(withdrawn["status"], "awaiting_analyst")
        self.assertNotIn("analyst", withdrawn["data"]["signatures"])
        self.assertIn("authorizer", withdrawn["data"]["signatures"])
        self.assertEqual(withdrawn["data"]["signatures"]["authorizer"]["signer"], "carol")

        # The analyst can re-sign, returning the result to released.
        resigned = self.service.transition(self.alice, result["id"], "sign_analyst", {})
        self.assertEqual(resigned["status"], "released")

    def test_version_conflict_for_late_second_signer(self):
        result = self._result()
        self.service.transition(self.alice, result["id"], "sign_analyst", {})
        stale = self.service.get(result["id"])["version"]
        auth1 = Actor("auth1", "authorizer")
        auth2 = Actor("auth2", "authorizer")

        first = self.service.transition(
            auth1, result["id"], "sign_authorizer", {}, expected_version=stale
        )
        self.assertEqual(first["status"], "released")
        with self.assertRaises(ConflictError):
            self.service.transition(
                auth2, result["id"], "sign_authorizer", {}, expected_version=stale
            )

    def test_concurrent_second_signers_first_wins_late_gets_conflict(self):
        result = self._result()
        self.service.transition(self.alice, result["id"], "sign_analyst", {})
        auth1 = Actor("auth1", "authorizer")
        auth2 = Actor("auth2", "authorizer")

        versions = []
        barrier = threading.Barrier(2)

        def sign(actor):
            entity = self.service.get(result["id"])
            barrier.wait()
            versions.append(entity["version"])
            return self.service.transition(
                actor, result["id"], "sign_authorizer", {},
                expected_version=entity["version"],
            )

        outcomes = []
        errors = []

        def run(actor):
            try:
                outcomes.append(sign(actor))
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=run, args=(auth1,))
        t2 = threading.Thread(target=run, args=(auth2,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["status"], "released")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ConflictError)
        self.assertEqual(len(set(versions)), 1)

    def test_audit_trail_records_signatures_and_delegation(self):
        delegation = self._delegate("analyst", "alice", "bob")
        result = self._result()
        self.service.transition(self.bob, result["id"], "sign_analyst", {})
        self.service.transition(self.carol, result["id"], "sign_authorizer", {})

        result_audit = self.service.audit_log(result["id"])
        actions = [entry["action"] for entry in result_audit]
        self.assertIn("sign_analyst", actions)
        self.assertIn("sign_authorizer", actions)
        for entry in result_audit:
            self.assertTrue(entry["created_at"])

        delegation_audit = self.service.audit_log(delegation["id"])
        self.assertTrue(any(entry["action"] == "create" for entry in delegation_audit))


if __name__ == "__main__":
    unittest.main()
