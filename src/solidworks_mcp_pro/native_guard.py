"""Compatibility entry point for the independent session transaction API."""


def run_guarded(session, function, *args, **kwargs):
    return session.run_guarded(function, *args, **kwargs)
