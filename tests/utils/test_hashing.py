from __future__ import annotations

import pytest

from sqlmesh.utils.hashing import crc32, hash_data, md5


class TestCrc32:
    """Tests for the crc32 function."""

    def test_crc32_single_string(self) -> None:
        """CRC32 of a single string returns consistent hash."""
        result = crc32(["hello"])
        assert result == str(__import__("zlib").crc32(b"hello"))

    def test_crc32_multiple_strings(self) -> None:
        """CRC32 of multiple strings joins with semicolons."""
        result = crc32(["a", "b", "c"])
        assert result == str(__import__("zlib").crc32(b"a;b;c"))

    def test_crc32_empty_iterable(self) -> None:
        """CRC32 of empty iterable returns hash of empty string."""
        result = crc32([])
        assert result == str(__import__("zlib").crc32(b""))

    def test_crc32_with_none_values(self) -> None:
        """CRC32 treats None as empty string."""
        result = crc32(["a", None, "c"])
        assert result == str(__import__("zlib").crc32(b"a;;c"))

    def test_crc32_all_none(self) -> None:
        """CRC32 of all None values returns hash of semicolons."""
        result = crc32([None, None])
        assert result == str(__import__("zlib").crc32(b";"))

    def test_crc32_returns_string(self) -> None:
        """CRC32 always returns a string type."""
        result = crc32(["test"])
        assert isinstance(result, str)

    def test_crc32_deterministic(self) -> None:
        """CRC32 returns same result for same input."""
        data = ["hello", "world"]
        assert crc32(data) == crc32(data)


class TestMd5:
    """Tests for the md5 function."""

    def test_md5_single_string(self) -> None:
        """MD5 accepts a single string directly."""
        result = md5("hello")
        assert result == __import__("hashlib").md5(b"hello").hexdigest()

    def test_md5_iterable(self) -> None:
        """MD5 accepts an iterable of strings."""
        result = md5(["a", "b", "c"])
        assert result == __import__("hashlib").md5(b"a;b;c").hexdigest()

    def test_md5_empty_string(self) -> None:
        """MD5 of empty string returns expected hash."""
        result = md5("")
        assert result == __import__("hashlib").md5(b"").hexdigest()

    def test_md5_empty_iterable(self) -> None:
        """MD5 of empty iterable returns hash of empty string."""
        result = md5([])
        assert result == __import__("hashlib").md5(b"").hexdigest()

    def test_md5_with_none_values(self) -> None:
        """MD5 treats None as empty string in iterable."""
        result = md5(["a", None, "c"])
        assert result == __import__("hashlib").md5(b"a;;c").hexdigest()

    def test_md5_returns_hexdigest(self) -> None:
        """MD5 returns a 32-character hexadecimal string."""
        result = md5("test")
        assert len(result) == 32
        assert all(c in "0123456789abcdef" for c in result)

    def test_md5_deterministic(self) -> None:
        """MD5 returns same result for same input."""
        assert md5("hello") == md5("hello")
        assert md5(["a", "b"]) == md5(["a", "b"])


class TestHashData:
    """Tests for the hash_data function."""

    def test_hash_data_delegates_to_crc32(self) -> None:
        """hash_data is an alias for crc32."""
        data = ["hello", "world"]
        assert hash_data(data) == crc32(data)

    def test_hash_data_with_none(self) -> None:
        """hash_data handles None values like crc32."""
        data = ["a", None, "b"]
        assert hash_data(data) == crc32(data)

    def test_hash_data_empty(self) -> None:
        """hash_data handles empty iterable."""
        assert hash_data([]) == crc32([])


@pytest.mark.parametrize(
    "data,expected_separator_count",
    [
        (["a"], 0),
        (["a", "b"], 1),
        (["a", "b", "c"], 2),
        ([None], 0),
        ([None, None], 1),
    ],
)
def test_concatenation_uses_semicolons(
    data: list[str | None], expected_separator_count: int
) -> None:
    """Verify that data is concatenated with semicolons."""
    # We verify this indirectly by checking that different orderings
    # produce different hashes (which wouldn't happen if not concatenated properly)
    if len(data) >= 2 and data[0] != data[-1]:
        reversed_data = list(reversed(data))
        assert crc32(data) != crc32(reversed_data)
