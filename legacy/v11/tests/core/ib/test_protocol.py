"""
tests/core/ib/test_protocol.py — Unit tests for core.ib.protocol

These tests cover pure functions that don't require a live IB Gateway.
Use `pytest -m "not requires_gateway"` to skip integration tests.
"""

import io
import socket
import struct
import time
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest

from core.ib import protocol as p


# ---------------------------------------------------------------------------
# encode_msg / decode_msg_fields
# ---------------------------------------------------------------------------

class TestEncodeMsg:
    """Tests for encode_msg — pure function, no mocking needed."""

    def test_basic_message(self):
        """Encode a simple message with two fields."""
        result = p.encode_msg(71, 2, 199, "")
        # 4-byte length prefix + payload
        length = struct.unpack(">I", result[:4])[0]
        payload = result[4:]
        assert length == len(payload)
        # Fields joined with \0, trailing \0
        assert payload == b"71\x002\x00199\x00\x00"

    def test_message_type_only(self):
        """Encode a message with just a type and one field."""
        result = p.encode_msg(59, 1, 1)
        length = struct.unpack(">I", result[:4])[0]
        payload = result[4:]
        assert length == len(payload)
        assert payload == b"59\x001\x001\x00"

    def test_empty_fields_become_empty_string(self):
        """None fields should become empty strings."""
        result = p.encode_msg(71, 2, 199, None)
        length = struct.unpack(">I", result[:4])[0]
        payload = result[4:]
        assert payload == b"71\x002\x00199\x00\x00"

    def test_length_prefix_is_big_endian(self):
        """Length prefix must be big-endian (network byte order)."""
        # encode_msg(1, "test") → payload = "1\0test\0" = 7 bytes
        result = p.encode_msg(1, "test")
        length = struct.unpack(">I", result[:4])[0]
        assert length == 7  # "1\0test\0" is 7 bytes
        # Verify big-endian: 7 = 0x00000007
        assert result[0:4] == b"\x00\x00\x00\x07"


class TestDecodeMsgFields:
    """Tests for decode_msg_fields — pure function."""

    def test_single_field(self):
        """Decode a message with one field after the type byte."""
        # payload = msg_type(1 byte) + field1 + \0
        payload = b"\x47" + b"hello\x00"
        fields = p.decode_msg_fields(payload)
        assert len(fields) == 1
        assert fields[0] == b"hello"

    def test_multiple_fields(self):
        """Decode a message with multiple null-delimited fields."""
        payload = b"\x49" + b"5\x001\x00DU0000000\x00"
        fields = p.decode_msg_fields(payload)
        assert len(fields) == 3
        assert fields[0] == b"5"
        assert fields[1] == b"1"
        assert fields[2] == b"DU0000000"

    def test_trailing_nulls_stripped(self):
        """Trailing nulls should be stripped before splitting."""
        payload = b"\x57" + b"\x001\x001\x00\x00\x00"
        fields = p.decode_msg_fields(payload)
        # Should strip trailing \0\0\0 then split
        assert fields == [b"", b"1", b"1"]

    def test_empty_payload_returns_single_empty_field(self):
        """Empty payload should return a single empty field (b'').

        Python's str.split on empty string returns [''], same for bytes.
        """
        fields = p.decode_msg_fields(b"")
        assert fields == [b""]

    def test_malformed_payload_returns_empty_list(self):
        """Malformed payload should not raise — return empty list."""
        # Binary data that can't be split safely
        fields = p.decode_msg_fields(b"\x00" * 100)
        # Should not raise
        assert isinstance(fields, list)


# ---------------------------------------------------------------------------
# safe_close
# ---------------------------------------------------------------------------

class TestSafeClose:
    """Tests for safe_close."""

    def test_none_socket(self):
        """safe_close(None) should not raise."""
        p.safe_close(None)  # Should not raise

    def test_normal_close(self):
        """safe_close should call shutdown then close."""
        mock_sock = MagicMock()
        p.safe_close(mock_sock)
        mock_sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        mock_sock.close.assert_called_once()

    def test_shutdown_raises_ignored(self):
        """If shutdown raises OSError, close() should still be called."""
        mock_sock = MagicMock()
        mock_sock.shutdown.side_effect = OSError("not connected")
        p.safe_close(mock_sock)  # Should not raise
        mock_sock.close.assert_called_once()

    def test_close_raises_ignored(self):
        """If close raises OSError, it should be ignored."""
        mock_sock = MagicMock()
        mock_sock.close.side_effect = OSError("already closed")
        p.safe_close(mock_sock)  # Should not raise


# ---------------------------------------------------------------------------
# recv_exact
# ---------------------------------------------------------------------------

class TestRecvExact:
    """Tests for recv_exact."""

    def test_exact_bytes_received(self):
        """Should return exactly n bytes."""
        mock_sock = MagicMock()
        # Each chunk must be smaller than the remaining buffer so recv is called
        # multiple times (simulating a stream arriving in pieces).
        # "hello!" = 6 bytes split into 3 chunks.
        mock_sock.recv.side_effect = [b"he", b"ll", b"o!"]
        result = p.recv_exact(mock_sock, 6)
        assert result == b"hello!"
        assert mock_sock.recv.call_count == 3

    def test_single_recv_sufficient(self):
        """If recv returns all bytes at once, should not call recv again."""
        mock_sock = MagicMock()
        mock_sock.recv.return_value = b"hello"
        result = p.recv_exact(mock_sock, 5)
        assert result == b"hello"
        assert mock_sock.recv.call_count == 1

    def test_eof_returns_none(self):
        """If recv returns empty bytes (EOF), should return None."""
        mock_sock = MagicMock()
        mock_sock.recv.return_value = b""
        result = p.recv_exact(mock_sock, 5)
        assert result is None


# ---------------------------------------------------------------------------
# parse_server_time
# ---------------------------------------------------------------------------

class TestParseServerTime:
    """Tests for parse_server_time."""

    def test_standard_format_with_tz(self):
        """Standard format with timezone."""
        dt = p.parse_server_time("20260409 09:22:28 EST")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 4
        assert dt.day == 9
        assert dt.hour == 9
        assert dt.minute == 22
        assert dt.second == 28

    def test_standard_format_edt(self):
        """EDT timezone."""
        dt = p.parse_server_time("20260409 10:22:28 EDT")
        assert dt is not None
        assert dt.hour == 10

    def test_empty_returns_none(self):
        """Empty string returns None."""
        assert p.parse_server_time("") is None
        assert p.parse_server_time("?") is None
        assert p.parse_server_time(None) is None

    def test_malformed_returns_none(self):
        """Malformed string returns None without raising."""
        assert p.parse_server_time("not a date") is None
        assert p.parse_server_time("2026-04-09") is None

    def test_parses_without_tz(self):
        """String without timezone should still parse."""
        dt = p.parse_server_time("20260409 09:22:28")
        assert dt is not None
        assert dt.hour == 9


# ---------------------------------------------------------------------------
# MSG type constants
# ---------------------------------------------------------------------------

class TestConstants:
    """Tests for protocol constants."""

    def test_msg_type_values(self):
        """Verify MSG type constants match known IB values."""
        assert p.MSG_START_API == 71
        assert p.MSG_ACCOUNT_LIST == 49
        assert p.MSG_MARKET_DATA_TYPE == 57
        assert p.MSG_REQ_MARKET_DATA_TYPE == 59
        assert p.MSG_SERVER_MESSAGE == 52

    def test_md_type_labels(self):
        """Verify market data type labels."""
        assert p.MD_TYPE_LABELS[1] == "Live"
        assert p.MD_TYPE_LABELS[2] == "Frozen"
        assert p.MD_TYPE_LABELS[3] == "Delayed"
        assert p.MD_TYPE_LABELS[4] == "Delay-Freeze"

    def test_version_string_format(self):
        """Version string should be 'v{min}..{max}'."""
        assert p.VERSION_STRING.startswith("v")
        assert ".." in p.VERSION_STRING
        assert p.MIN_CLIENT_VERSION == 157
        assert p.MAX_CLIENT_VERSION == 176
