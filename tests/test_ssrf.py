# tests/test_ssrf.py
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import twitch_bot

class TestValidateLLMEndpoint(unittest.TestCase):
    def test_aws_metadata_endpoint_blocked(self):
        self.assertFalse(twitch_bot._validate_llm_endpoint("http://169.254.169.254/latest/meta-data/"))

    def test_gcp_metadata_endpoint_blocked(self):
        self.assertFalse(twitch_bot._validate_llm_endpoint("http://metadata.google.internal/"))

    def test_azure_metadata_endpoint_blocked(self):
        self.assertFalse(twitch_bot._validate_llm_endpoint("http://169.254.169.254/metadata/instance"))

    def test_localhost_ollama_allowed(self):
        self.assertTrue(twitch_bot._validate_llm_endpoint("http://localhost:11434/v1/chat/completions"))

    def test_local_ip_lm_studio_allowed(self):
        self.assertTrue(twitch_bot._validate_llm_endpoint("http://127.0.0.1:1234/v1/chat/completions"))

    def test_openai_allowed(self):
        self.assertTrue(twitch_bot._validate_llm_endpoint("https://api.openai.com/v1/chat/completions"))

    def test_empty_url_blocked(self):
        self.assertFalse(twitch_bot._validate_llm_endpoint(""))

    def test_non_http_scheme_blocked(self):
        self.assertFalse(twitch_bot._validate_llm_endpoint("file:///etc/passwd"))

if __name__ == "__main__":
    unittest.main()
