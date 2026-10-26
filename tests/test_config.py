import json
import os
import stat
import unittest
from pathlib import Path
from unittest import mock

from pocketagent.config import AppConfig, ConfigError, ModelProfile, Settings, config_path
from pocketagent.permissions import ApprovalMode
from tests.helpers import TemporaryDirectories

DS_PROFILE = {
    "id": "ds",
    "name": "DS 4.1 Flash",
    "model": "deepseek-v4.1-flash",
    "base_url": "https://new-api.example/v1",
    "api_key": "sk-1234567890abcdef",
    "reasoning_effort": "low",
    "context_window_tokens": 64000,
}
QWEN_PROFILE = {"id": "qwen", "name": "Qwen 27B", "model": "Qwen3.8-27B", "extra_body": '{"top_k": 20}'}


class AppConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.path = config_path(self.directories.home_dir)

    def tearDown(self) -> None:
        self.directories.cleanup()

    def test_save_and_load_round_trip_with_private_permissions(self) -> None:
        config = AppConfig.from_dict({"models": [DS_PROFILE, QWEN_PROFILE], "default_model": "qwen", "max_steps": 30})

        config.save(self.path)
        loaded = AppConfig.load(self.path)

        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(loaded.default_profile().name, "Qwen 27B")
        self.assertEqual(loaded.find_model("ds").reasoning_effort, "low")
        self.assertEqual(loaded.find_model("ds").context_window_tokens, 64000)
        self.assertEqual(loaded.find_model("qwen").extra_body, {"top_k": 20})
        self.assertEqual(loaded.max_steps, 30)

    def test_missing_file_gives_empty_config(self) -> None:
        config = AppConfig.load(self.path)

        self.assertEqual(config.models, [])
        self.assertIsNone(config.default_profile())

    def test_public_dict_hides_api_key(self) -> None:
        config = AppConfig.from_dict({"models": [DS_PROFILE]})

        public_model = config.public_dict()["models"][0]

        self.assertNotIn("api_key", public_model)
        self.assertEqual(public_model["api_key_hint"], "sk-123…cdef")
        self.assertNotIn("sk-1234567890abcdef", json.dumps(config.public_dict()))

    def test_find_model_by_id_name_or_model(self) -> None:
        config = AppConfig.from_dict({"models": [DS_PROFILE]})

        for key in ("ds", "DS 4.1 Flash", "deepseek-v4.1-flash"):
            with self.subTest(key=key):
                self.assertEqual(config.find_model(key).id, "ds")
        self.assertIsNone(config.find_model("gpt"))

    def test_validation_errors(self) -> None:
        invalid_configs = {
            "缺模型名": {"models": [{"name": "x"}]},
            "名称重复": {"models": [{"name": "same", "model": "a"}, {"name": "same", "model": "b"}]},
            "上下文不是数字": {"models": [{"model": "a", "context_window_tokens": "很长"}]},
            "额外参数不是对象": {"models": [{"model": "a", "extra_body": "[1, 2]"}]},
            "审批模式不对": {"approval_mode": "yolo"},
        }
        for label, data in invalid_configs.items():
            with self.subTest(label), self.assertRaises(ConfigError):
                AppConfig.from_dict(data)

    def test_unknown_default_falls_back_to_first_model(self) -> None:
        config = AppConfig.from_dict({"models": [DS_PROFILE], "default_model": "deleted"})

        self.assertEqual(config.default_model, "ds")


class SettingsLoadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.environment = {"POCKETAGENT_HOME": str(self.directories.home_dir)}

    def tearDown(self) -> None:
        self.directories.cleanup()

    def write_config(self, data: dict) -> None:
        AppConfig.from_dict(data).save(config_path(self.directories.home_dir))

    def load(self, **overrides) -> Settings:
        with mock.patch.dict(os.environ, self.environment, clear=True):
            return Settings.load(**overrides)

    def test_environment_is_used_when_no_model_configured(self) -> None:
        self.environment.update(
            {"POCKETAGENT_MODEL": "env-model", "OPENAI_API_KEY": "sk-env", "POCKETAGENT_CONTEXT_WINDOW": "32000"}
        )

        settings = self.load()

        self.assertEqual(settings.model, "env-model")
        self.assertEqual(settings.api_key, "sk-env")
        self.assertEqual(settings.context_window_tokens, 32000)
        self.assertIsNone(settings.model_profile_id)

    def test_configured_default_model_wins_over_environment(self) -> None:
        self.write_config({"models": [DS_PROFILE, QWEN_PROFILE], "default_model": "ds", "approval_mode": "auto_edit"})
        self.environment.update({"POCKETAGENT_MODEL": "env-model", "POCKETAGENT_API_KEY": "sk-env"})

        settings = self.load()

        self.assertEqual(settings.model, "deepseek-v4.1-flash")
        self.assertEqual(settings.api_key, "sk-1234567890abcdef")
        self.assertEqual(settings.reasoning_effort, "low")
        self.assertEqual(settings.context_window_tokens, 64000)
        self.assertEqual(settings.approval_mode, ApprovalMode.AUTO_EDIT)
        self.assertEqual(settings.display_model(), "DS 4.1 Flash（deepseek-v4.1-flash）")

    def test_command_line_picks_model_and_overrides_mode(self) -> None:
        self.write_config({"models": [DS_PROFILE, QWEN_PROFILE], "default_model": "ds"})

        by_name = self.load(model="Qwen 27B", approval_mode=ApprovalMode.FULL_AUTO)
        raw_model = self.load(model="deepseek-v5")

        self.assertEqual(by_name.model, "Qwen3.8-27B")
        self.assertEqual(by_name.approval_mode, ApprovalMode.FULL_AUTO)
        # 不是配置里的模型：用默认模型的接口，只换模型名
        self.assertEqual(raw_model.model, "deepseek-v5")
        self.assertEqual(raw_model.base_url, "https://new-api.example/v1")

    def test_broken_config_file_raises(self) -> None:
        path = config_path(self.directories.home_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

        with self.assertRaises(ConfigError):
            self.load()


class ModelProfileTest(unittest.TestCase):
    def test_name_defaults_to_model_and_id_is_generated(self) -> None:
        profile = ModelProfile.from_dict({"model": "glm-5.3-flash", "temperature": "0.3"})

        self.assertEqual(profile.name, "glm-5.3-flash")
        self.assertEqual(len(profile.id), 8)
        self.assertEqual(profile.temperature, 0.3)

    def test_use_profile_copies_extra_body(self) -> None:
        profile = ModelProfile.from_dict(QWEN_PROFILE)
        settings = Settings(workspace=Path("."))

        settings.use_profile(profile)
        settings.extra_body["changed"] = True

        self.assertEqual(profile.extra_body, {"top_k": 20})


if __name__ == "__main__":
    unittest.main()
