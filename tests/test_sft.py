"""
Basic unit tests for SFT helpers.
Run: pytest -q
"""

from tools.coros_uploader import crc16_ccitt_false, coros_byte_sum, create_coros_raw_watchface_envelope, create_coros_sft_start_command, create_coros_sft_data_windows, COROS_SFT_PACKET_DATA_BYTES

def test_crc16():
    data = b"123456789"
    assert crc16_ccitt_false(data) == 0x29b1

def test_byte_sum():
    assert coros_byte_sum(b"\x01\x02\x03") == 6

def test_envelope_and_windows():
    payload = b"614A" + b"\x00"*20 + b"hello world"*100
    # craft a fake legacy header matching format: keep size consistent
    # this test focuses on envelope creation rather than full validation
    envelope = create_coros_raw_watchface_envelope(payload)
    assert len(envelope) == 16
    block = {"offset": 0, "remainingBytes": len(payload) + len(envelope), "crc16": 0, "byteSum": 0, "bytes": (envelope + payload)[:12288]}
    start = create_coros_sft_start_command(block)
    assert start[0] == 0x78
    windows = create_coros_sft_data_windows(block)
    # packets per window should be >=1
    assert len(windows) >= 1
    for win in windows:
        for pkt in win:
            assert pkt[0] == 0x78
            assert pkt[2] == 0x08
