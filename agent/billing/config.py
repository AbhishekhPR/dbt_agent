"""Polar configuration, validated at boot.

Follows the same rule as agent/api/clerk_identity.py's ``ClerkSettings``: a
deployment with no Polar configuration starts and serves everything else, but a
deployment with BROKEN Polar configuration refuses to start. A billing endpoint
that authenticates people and then fails on the first checkout is worse than one
that was never enabled, because it fails on a customer rather than on us.

###################################################################
# NOTHING IN HERE IS EVER RETURNED TO A BROWSER OR LOGGED.        #
###################################################################

The access token and the webhook secret are ``repr=False`` dataclass fields, so
they cannot reach a log line through an accidental ``%r`` of the settings
object. No function in this package logs a secret, a token, or a signature; the
webhook route logs the failure category and never the header it rejected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

#: Polar's two environments. Their data, tokens and webhook secrets are fully
#: isolated from each other, so which one a deployment talks to is configuration
#: and never a default that could silently be the wrong one in production.
SERVER_PRODUCTION = "production"
SERVER_SANDBOX = "sandbox"

API_BASE_URLS = {
    SERVER_PRODUCTION: "https://api.polar.sh",
    SERVER_SANDBOX: "https://sandbox-api.polar.sh",
}

#: Prefix of a Polar Organization Access Token. Checked only to catch the
#: common paste error of putting a publishable/other credential here; the token
#: itself is never inspected further and never leaves this process except as an
#: Authorization header to Polar.
_TOKEN_PREFIXES = ("polar_oat_", "polar_pat_")

MAX_GRACE_DAYS = 21


class PolarConfigurationError(ValueError):
    """Polar configuration is present but unusable. Fatal at boot."""


@dataclass(frozen=True)
class PolarSettings:
    access_token: str = field(repr=False)
    webhook_secret: str = field(repr=False)
    starter_product_id: str
    pro_product_id: str
    server: str = SERVER_PRODUCTION
    #: How long a `past_due` subscription keeps its plan while Polar retries the
    #: charge. Mirrors the organization-level "Grace period for benefit
    #: revocation" in Polar, which the API does not expose. Defaults to zero,
    #: which is what Polar itself defaults to.
    past_due_grace: timedelta = timedelta(0)

    @property
    def api_base_url(self) -> str:
        return API_BASE_URLS[self.server]

    @property
    def is_sandbox(self) -> bool:
        return self.server == SERVER_SANDBOX

    @classmethod
    def from_environ(cls, environ=None):
        """Build settings, or None when Polar is not configured at all.

        None is returned only when NOTHING is set. A partially configured
        deployment raises: half a billing integration is the state in which a
        customer reaches checkout and the webhook that would grant them their
        plan is never verified.
        """
        import os

        values = os.environ if environ is None else environ

        names = ("POLAR_ACCESS_TOKEN", "POLAR_WEBHOOK_SECRET",
                 "POLAR_STARTER_PRODUCT_ID", "POLAR_PRO_PRODUCT_ID")
        present = {name: _text(values.get(name)) for name in names}
        if not any(present.values()):
            extra = [name for name in ("POLAR_SERVER", "POLAR_PAST_DUE_GRACE_DAYS")
                     if _text(values.get(name))]
            if extra:
                raise PolarConfigurationError(
                    f"{extra[0]} is set but Polar billing is not configured; "
                    "set POLAR_ACCESS_TOKEN, POLAR_WEBHOOK_SECRET, "
                    "POLAR_STARTER_PRODUCT_ID and POLAR_PRO_PRODUCT_ID, or "
                    "unset it.")
            return None

        missing = sorted(name for name, value in present.items() if not value)
        if missing:
            raise PolarConfigurationError(
                "Polar billing is partially configured; missing "
                + ", ".join(missing))

        access_token = present["POLAR_ACCESS_TOKEN"]
        if not access_token.startswith(_TOKEN_PREFIXES):
            raise PolarConfigurationError(
                "POLAR_ACCESS_TOKEN does not look like a Polar organization "
                "access token (expected a polar_oat_ or polar_pat_ prefix).")

        server = _text(values.get("POLAR_SERVER")) or SERVER_PRODUCTION
        if server not in API_BASE_URLS:
            raise PolarConfigurationError(
                "POLAR_SERVER must be 'production' or 'sandbox'.")
        railway_environment = _text(values.get("RAILWAY_ENVIRONMENT_NAME"))
        if railway_environment == "production" and server != SERVER_PRODUCTION:
            raise PolarConfigurationError(
                "Railway production requires POLAR_SERVER=production; sandbox "
                "tokens, products, customers, subscriptions and webhooks are "
                "separate.")

        starter = present["POLAR_STARTER_PRODUCT_ID"]
        pro = present["POLAR_PRO_PRODUCT_ID"]
        if starter == pro:
            # Both plans pointing at one product would sell Pro at the Starter
            # price, or grant Pro to a Starter customer, depending on which way
            # round the mistake was made. Neither is recoverable at runtime.
            raise PolarConfigurationError(
                "POLAR_STARTER_PRODUCT_ID and POLAR_PRO_PRODUCT_ID must be "
                "different products.")
        for name, value in (("POLAR_STARTER_PRODUCT_ID", starter),
                            ("POLAR_PRO_PRODUCT_ID", pro)):
            if len(value) > 255:
                raise PolarConfigurationError(f"{name} is not a product id.")

        return cls(
            access_token=access_token,
            webhook_secret=present["POLAR_WEBHOOK_SECRET"],
            starter_product_id=starter,
            pro_product_id=pro,
            server=server,
            past_due_grace=_grace(values.get("POLAR_PAST_DUE_GRACE_DAYS")),
        )


def _text(value):
    if value is None or not isinstance(value, str):
        return None
    return value.strip() or None


def _grace(raw) -> timedelta:
    value = _text(raw)
    if value is None:
        return timedelta(0)
    try:
        days = int(value)
    except ValueError:
        raise PolarConfigurationError(
            "POLAR_PAST_DUE_GRACE_DAYS must be a whole number of days.") from None
    if days < 0 or days > MAX_GRACE_DAYS:
        # Polar's own retry schedule runs for 21 days and then revokes. A longer
        # grace here would keep granting a plan Polar has already given up on.
        raise PolarConfigurationError(
            "POLAR_PAST_DUE_GRACE_DAYS must be between 0 and "
            f"{MAX_GRACE_DAYS}.")
    return timedelta(days=days)
