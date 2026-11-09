"""The joint-share choice as a page header shows it (components/view_controls.html, joint_menu).

Joint share is one setting for the whole app (omsync.ledger.preferences), so every page whose
amounts it changes shows the same control. It is offered only when an account is joint.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from omsync.ledger.preferences import has_joint_accounts, joint_share_on

JOINT_HALF = "half"
JOINT_FULL = "full"


@dataclass(frozen=True)
class JointView:
    """half is True when joint accounts count at half."""

    half: bool

    @property
    def label(self) -> str:
        return "My half" if self.half else "Full"

    @property
    def words(self) -> str:
        """The choice in words, for the line under a page's title."""
        return "your half of joint accounts" if self.half else "joint accounts in full"


def joint_view(db: Session) -> JointView | None:
    """The control's state, or None when no account is joint, so the control is not shown."""
    return JointView(joint_share_on(db)) if has_joint_accounts(db) else None
