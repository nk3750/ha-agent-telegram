import requests

from ha_agent.config import HA_URL, HA_TOKEN


class HomeAssistantClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def get_state(self, entity_id: str) -> dict:
        resp = requests.get(f"{self.base_url}/api/states/{entity_id}", headers=self.headers)
        resp.raise_for_status()
        return resp.json()

    def get_all_states(self) -> list[dict]:
        resp = requests.get(f"{self.base_url}/api/states", headers=self.headers)
        resp.raise_for_status()
        return resp.json()

    def call_service(self, domain: str, service: str, entity_id: str | None = None, data: dict | None = None) -> list[dict]:
        payload = data.copy() if data else {}
        if entity_id:
            payload["entity_id"] = entity_id
        resp = requests.post(
            f"{self.base_url}/api/services/{domain}/{service}",
            headers=self.headers,
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    def get_services(self) -> list[dict]:
        resp = requests.get(f"{self.base_url}/api/services", headers=self.headers)
        resp.raise_for_status()
        return resp.json()

    def render_template(self, template: str) -> str:
        resp = requests.post(
            f"{self.base_url}/api/template",
            headers=self.headers,
            json={"template": template},
        )
        resp.raise_for_status()
        return resp.text


ha = HomeAssistantClient(HA_URL, HA_TOKEN)
