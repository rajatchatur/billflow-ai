#!/usr/bin/env bash
# Prints a random session secret suitable for BILLFLOW_SESSION_SECRET.
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
