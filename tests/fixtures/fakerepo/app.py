"""Fixture app with a planted issue for audit-tooling tests."""


def handler(req):
    user_input = req.args["q"]
    # SECURITY-FIXTURE: unsanitized input reaches a dynamic call (command-injection stand-in)
    return run_command(user_input)
