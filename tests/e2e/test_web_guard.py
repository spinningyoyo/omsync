"""The session guard: the Origin check on state-changing requests."""

from fastapi.testclient import TestClient
from web_support import sign_in


def test_a_post_from_another_origin_is_forbidden(client: TestClient) -> None:
    sign_in(client)

    response = client.post(
        "/reports", headers={"Origin": "https://evil.example"}, follow_redirects=False
    )

    assert response.status_code == 403
