"""文本层测试：脱敏、分词、切块。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import text  # noqa: E402


class TestRedact(unittest.TestCase):
    def test_github_token(self):
        out = text.redact_secrets("ghp_IfVrZedcp2oA1xH9ecSXPF8I0T3uM40ireU 我的token")
        self.assertNotIn("ghp_", out)
        self.assertIn("已脱敏", out)

    def test_key_word_space_separated(self):
        out = text.redact_secrets("API Key owJxLXcTqnSqygANja2YMQbR 记一下")
        self.assertNotIn("owJxLXcTqnSqygANja2YMQbR", out)

    def test_colon_form(self):
        self.assertNotIn("hunter2pass", text.redact_secrets("password: hunter2pass"))

    def test_chinese_keyword(self):
        self.assertNotIn("abcdefgh12345678", text.redact_secrets("密钥 abcdefgh12345678"))

    def test_no_false_positive_on_normal_sentence(self):
        for sentence in [
            "token 过期了怎么办",
            "这个接口的 API Key 在哪里配置",
            "配对码是554120，IP是192.168.1.105:42329",
        ]:
            self.assertEqual(text.redact_secrets(sentence), sentence, "不该误伤：%s" % sentence)


class TestTokenize(unittest.TestCase):
    def test_chinese_bigrams(self):
        self.assertEqual(text.tokenize("知识库"), ["知识", "识库"])

    def test_mixed(self):
        tokens = text.tokenize("用 API 查知识库")
        self.assertIn("api", tokens)
        self.assertIn("知识", tokens)

    def test_fts_query_quotes(self):
        q = text.fts_query("退货政策")
        self.assertIn('"退货"', q)
        self.assertIn(" OR ", q)


class TestChunk(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(len(text.chunk_text("简短的一句话")), 1)

    def test_long_text_multiple_chunks(self):
        body = "。".join(["这是第%d句话，用来凑长度测试切块逻辑是否正常工作" % i for i in range(30)])
        chunks = text.chunk_text(body, size=120, overlap=20)
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(len(c) <= 200 for c in chunks))

    def test_noise_detection(self):
        self.assertTrue(text.is_noise("[图片]"))
        self.assertTrue(text.is_noise(""))
        self.assertFalse(text.is_noise("这是一个正常的讨论结论"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
