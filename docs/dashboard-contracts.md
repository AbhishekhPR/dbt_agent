# Dashboard and delivery contracts

Dashboard resources are tenant-scoped and expose evidence links, coverage,
decision, and status. GitHub, dashboard, and Slack each have an independent
delivery journal keyed by organization, repository, environment, channel, and
event. Duplicate events are acknowledged without duplicate delivery; retries
are bounded and dead-lettered. Slack is disabled by default and its failure
cannot alter GitHub or lifecycle decisions.
