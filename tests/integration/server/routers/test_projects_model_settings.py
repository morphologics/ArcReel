"""Reading and writing model_settings in the projects routes.

projects 路由的 model_settings 读写。"""

from tests.integration.server.routers.projects_router_support import (
    _FakePM,
    build_projects_client,
)


class TestModelSettingsApi:
    def test_create_project_with_model_settings(self, tmp_path, monkeypatch):
        fake_pm = _FakePM(tmp_path)
        client = build_projects_client(monkeypatch, fake_pm)
        with client:
            resp = client.post(
                "/api/v1/projects",
                json={
                    "generation_mode": "storyboard",
                    "name": "demo-res",
                    "title": "T",
                    "model_settings": {
                        "gemini-aistudio/veo-3.1-lite-generate-preview": {"resolution": "720p"},
                    },
                },
            )
            assert resp.status_code == 200
            # Verify directly from the create return value that model_settings was persisted
            # 直接从 create 返回值验证 model_settings 已持久化
            project = resp.json()["project"]
            assert project["model_settings"]["gemini-aistudio/veo-3.1-lite-generate-preview"]["resolution"] == "720p"
            # Also verify fake_pm's internal storage
            # 也验证 fake_pm 内部存储
            stored = fake_pm.project_data["demo-res"]
            assert stored["model_settings"]["gemini-aistudio/veo-3.1-lite-generate-preview"]["resolution"] == "720p"

    def test_patch_project_model_settings(self, tmp_path, monkeypatch):
        fake_pm = _FakePM(tmp_path)
        client = build_projects_client(monkeypatch, fake_pm)
        with client:
            # Create first (using the existing ready project)
            # 先创建（利用现有 ready 项目）
            resp = client.patch(
                "/api/v1/projects/ready",
                json={"model_settings": {"gemini-aistudio/veo-3.1": {"resolution": "1080p"}}},
            )
            assert resp.status_code == 200
            # Verify model_settings directly from the patch return value
            # 直接从 patch 返回值验证 model_settings
            project = resp.json()["project"]
            assert project["model_settings"]["gemini-aistudio/veo-3.1"]["resolution"] == "1080p"
            # Also verify fake_pm's internal storage
            # 也验证 fake_pm 内部存储
            stored = fake_pm.project_data["ready"]
            assert stored["model_settings"]["gemini-aistudio/veo-3.1"]["resolution"] == "1080p"
