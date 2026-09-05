import json
import os
import tempfile
import unittest

from src.config import Config


class ConfigPersistenceTests(unittest.TestCase):
    def test_multi_ai_defaults_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(os.path.join(directory, "config.json"))
            settings = config.to_dict()
            settings["ai_model"] = "多AI作答"

            self.assertEqual(settings["multi_ai_config_path"], "model_visible.ini")
            self.assertEqual(settings["multi_ai_timeout"], 20)
            self.assertEqual(config.save(settings), [])

            settings["multi_ai_timeout"] = 0
            self.assertIn("multi_ai_timeout 范围应为 1~300", config.save(settings))

    def test_save_writes_valid_config_without_leaving_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            config = Config(path)
            settings = config.to_dict()
            settings["quiz_refresh_interval"] = 2

            self.assertEqual(config.save(settings), [])
            with open(path, "r", encoding="utf-8") as file:
                saved = json.load(file)

            self.assertEqual(saved["quiz_refresh_interval"], 2)
            self.assertEqual(os.listdir(directory), ["config.json"])

    def test_persist_reports_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            config = Config(path)
            config.set("last_cookie_warn_date", "2026-09-01")

            self.assertTrue(config.persist())
            self.assertEqual(Config(path).get("last_cookie_warn_date"), "2026-09-01")


if __name__ == "__main__":
    unittest.main()
