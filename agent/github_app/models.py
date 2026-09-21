from dataclasses import dataclass


@dataclass(frozen=True)
class Repository:
    id: int
    owner: str
    name: str
    full_name: str


@dataclass(frozen=True)
class PullRequestEvent:
    delivery_id: str
    action: str
    installation_id: int
    repository: Repository
    pull_number: int
    head_sha: str
    base_sha: str
    sender_login: str
    #: Only meaningful for `closed`, which GitHub sends for BOTH a merge and a
    #: plain close and distinguishes only by this flag. Defaulted so every
    #: existing construction of this event keeps working unchanged.
    merged: bool = False

    @property
    def pr_state(self) -> str:
        """What this delivery says about the pull request itself.

        Kept here rather than in the runner because it is a reading of the
        GitHub payload, and the payload is this module's subject. It is not a
        review lifecycle state and never becomes one.
        """
        if self.action == "closed":
            return "MERGED" if self.merged else "CLOSED"
        return "OPEN"


@dataclass(frozen=True)
class InstallationEvent:
    """A GitHub App installation lifecycle delivery.

    Carries facts about the INSTALLATION and nothing about tenancy. Nothing in
    a webhook payload identifies a Relium tenant, and the fields that look like
    they might — the account login, the sender — are attacker-choosable names,
    not identities. The tenant binding is established elsewhere, by the
    verified Setup flow.
    """

    delivery_id: str
    action: str
    installation_id: int
    app_id: int | None
    account_id: int
    account_login: str
    account_type: str
    repository_selection: str | None
    sender_login: str | None
