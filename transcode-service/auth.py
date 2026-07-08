"""
auth.py — Pub/Sub push OIDC/JWT token verification.

Cloud Run Pub/Sub push subscriptions send an Authorization header with a
Google-signed OIDC token. We verify this token's signature and audience
before trusting the request.

Reference:
  https://cloud.google.com/pubsub/docs/authenticate-push-subscriptions
"""

import logging
from typing import Optional

import google.auth.transport.requests
from google.oauth2 import id_token as google_id_token

logger = logging.getLogger(__name__)

# Reusable request object (maintains a connection pool)
_http_request = google.auth.transport.requests.Request()


def verify_pubsub_token(auth_header: str, expected_audience: str) -> bool:
    """
    Verify the Bearer OIDC token in the Authorization header.

    Args:
      auth_header:       Full value of the 'Authorization' header.
      expected_audience: The Cloud Run service URL that Pub/Sub was configured
                         with as the push endpoint (must match token's 'aud' claim).

    Returns:
      True if the token is valid and matches the expected audience.
      False otherwise.
    """
    if not auth_header.startswith("Bearer "):
        logger.warning("Missing or malformed Bearer token in Authorization header")
        return False

    token = auth_header.split("Bearer ", 1)[1].strip()

    if not expected_audience:
        logger.error(
            "PUBSUB_AUDIENCE env var not set — cannot verify token audience. "
            "Set SKIP_AUTH=true only in local dev."
        )
        return False

    try:
        # google.oauth2.id_token.verify_oauth2_token fetches Google's public
        # keys automatically and verifies signature + expiry + audience.
        claims = google_id_token.verify_oauth2_token(
            token,
            _http_request,
            audience=expected_audience,
        )
        logger.debug(
            "Token verified: email=%s sub=%s",
            claims.get("email"),
            claims.get("sub"),
        )
        return True
    except Exception as exc:
        logger.warning("Token verification failed: %s", exc)
        return False
