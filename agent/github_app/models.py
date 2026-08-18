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
