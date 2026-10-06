from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class DomainError(Exception):
    """Base error for domain failures."""


class ValidationError(DomainError):
    """Input does not satisfy a domain rule."""


class PermissionDenied(DomainError):
    """Actor is not allowed to perform the action."""


class NotFoundError(DomainError):
    """Requested record does not exist."""


class ConflictError(DomainError):
    """A version or uniqueness constraint was violated."""


class InvalidTransition(DomainError):
    """The requested state transition is not valid."""


class Role(str, Enum):
    viewer = "viewer"
    admin = "admin"
    technician = "technician"
    metrology = "metrology"
    authorizer = "authorizer"
    analyst = "analyst"


@dataclass
class Actor:
    """Authenticated caller.

    For a delegated action the rule engine resolves the header actor into an
    effective actor: ``user_id`` becomes the principal (post holder) while
    ``agent_id`` keeps the physical person who pressed the button.
    """

    user_id: str
    role: str
    on_behalf_of: Optional[str] = None
    delegation_id: Optional[str] = None
    agent_id: Optional[str] = None

    @property
    def principal_id(self):
        """Name the operation is recorded under (post holder)."""
        return self.on_behalf_of or self.user_id

    @property
    def physical_id(self):
        """Person who actually performed the operation."""
        return self.agent_id or self.user_id

    @property
    def is_delegated(self):
        return bool(self.delegation_id)

    @classmethod
    def from_headers(cls, headers):
        user_id = headers.get("X-User-Id", "anonymous")
        role = headers.get("X-Role", "viewer")
        if role not in {item.value for item in Role}:
            raise PermissionDenied("unknown role: " + role)
        on_behalf_of = headers.get("X-On-Behalf-Of") or None
        delegation_hint = headers.get("X-Delegation-Id") or None
        return cls(
            user_id=user_id,
            role=role,
            on_behalf_of=on_behalf_of,
            delegation_id=delegation_hint if on_behalf_of else None,
        )


@dataclass
class Entity:
    id: str
    kind: str
    status: str
    version: int
    data: Dict[str, Any]
    created_by: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row["id"],
            kind=row["kind"],
            status=row["status"],
            version=row["version"],
            data=row["data"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
