"""Polar billing: configuration, plan resolution, the Polar boundary, and the
one service the API routes call.

Nothing in this package decides authorization. The tenant is resolved from a
verified Clerk token by the route layer, exactly as onboarding does, and every
function here takes that tenant id as an argument rather than deriving one from
anything a browser or a webhook payload said.
"""
