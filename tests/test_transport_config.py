"""Connection encryption policy stays explicit and endpoint-specific."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from src.config import ConfigError, load_config
from src.database import connect


class TransportConfigTests(unittest.TestCase):
    def setUp(self):
        self.raw = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(encoding="utf-8"))
        for server in self.raw["servers"]:
            server.pop("ssl_disabled", None)

    def load(self, raw):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            return load_config(path)

    def test_missing_option_preserves_tls_negotiation(self):
        config = self.load(self.raw)
        self.assertFalse(config.servers[0].ssl_disabled)
        self.assertFalse(config.mysql.ssl_disabled)

    def test_plaintext_is_configured_per_server(self):
        self.raw["servers"][0]["ssl_disabled"] = True
        other = copy.deepcopy(self.raw["servers"][0])
        other.update(name="other", ssl_disabled=False)
        self.raw["servers"].append(other)
        config = self.load(self.raw)
        self.assertTrue(config.servers[0].ssl_disabled)
        self.assertFalse(config.servers[1].ssl_disabled)

    def test_string_boolean_is_rejected(self):
        self.raw["servers"][0]["ssl_disabled"] = "true"
        with self.assertRaises(ConfigError):
            self.load(self.raw)

    def test_explicit_plaintext_reaches_the_driver(self):
        self.raw["servers"][0]["ssl_disabled"] = True
        config = self.load(self.raw)
        with patch("src.database._SelectOnlyConnection") as driver:
            connect(config, config.servers[0])
        self.assertIs(driver.call_args.kwargs["ssl_disabled"], True)

    def test_legacy_mysql_option_reaches_both_config_views(self):
        endpoint = self.raw.pop("servers")[0]
        endpoint.update(database="business", ssl_disabled=True)
        self.raw["mysql"] = endpoint
        config = self.load(self.raw)
        self.assertTrue(config.mysql.ssl_disabled)
        self.assertTrue(config.servers[0].ssl_disabled)
        with patch("src.database._SelectOnlyConnection") as driver:
            connect(config)
        self.assertIs(driver.call_args.kwargs["ssl_disabled"], True)


if __name__ == "__main__":
    unittest.main()
