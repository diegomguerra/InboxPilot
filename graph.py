import requests

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"

def graph_get(access_token: str, path: str, params: dict | None = None):
    r = requests.get(
        GRAPH_ROOT + path,
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()

def graph_post(access_token: str, path: str, json_body: dict):
    r = requests.post(
        GRAPH_ROOT + path,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json=json_body,
        timeout=30,
    )
    r.raise_for_status()
    return r.json() if r.text else {}

def graph_patch(access_token: str, path: str, json_body: dict):
    r = requests.patch(
        GRAPH_ROOT + path,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json=json_body,
        timeout=30,
    )
    r.raise_for_status()
    return r.json() if r.text else {}

def graph_delete(access_token: str, path: str):
    r = requests.delete(
        GRAPH_ROOT + path,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    r.raise_for_status()
    return {}
