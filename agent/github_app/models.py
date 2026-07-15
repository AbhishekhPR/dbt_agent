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
