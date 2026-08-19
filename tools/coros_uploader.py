"""
COROS uploader and BLE SFT transport.

Modes:
  host  - upload file to transfer.sh or anonfiles and generate QR
  usb   - copy file to mount path
  ble   - raw SFT transfer to a COROS watch using bleak

Usage examples:
  python tools/coros_uploader.py --file myface.zip --mode host --host transfer.sh --qr qr.png
  python tools/coros_uploader.py --file legacy614a.bin --mode usb --mount /media/watch
  python tools/coros_uploader.py --file legacy614a.bin --mode ble --device-address AA:BB:CC:DD:EE:FF

Dependencies:
  pip install -r tools/requirements.txt
"""

import argparse
import asyncio
import math
import os
import shutil
import struct
import sys
from typing import List

# HTTP upload and QR
import requests
try:
    import qrcode
except Exception:
    qrcode = None

# BLE client (async)
try:
    from bleak import BleakClient, BleakScanner
except Exception:
    BleakClient = None
    BleakScanner = None

# ---- Ported SFT / utility functions (from CorosLink's corosRawWatchfaceTransfer.ts) ----

COROS_SFT_DATA_INDEX = 0x08
COROS_SFT_BLOCK_SIZE = 12_288
COROS_SFT_PACKET_BYTES = 240
COROS_SFT_PACKET_DATA_BYTES = COROS_SFT_PACKET_BYTES - 3
COROS_SFT_PACKETS_PER_WINDOW = 26
COROS_SFT_PROTOCOL_VERSION = 0x01

LEGACY_HEADER_BYTES = 0x12
LEGACY_MAGIC = b"614A"
TRANSFER_PREFIX = bytes([0x48, 0x46, 0x00, 0x08, 0x00, 0x00, 0x00, 0x00])

def read_uint32_le(b: bytes, offset: int) -> int:
    return struct.unpack_from("<I", b, offset)[0]

def write_uint32_le(barray: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<I", barray, offset, value)

def crc16_ccitt_false(data: bytes) -> int:
    crc = 0xffff
    for byte in data:
        crc ^= (byte << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xffff
            else:
                crc = (crc << 1) & 0xffff
    return crc

def coros_byte_sum(data: bytes) -> int:
    s = 0
    for b in data:
        s = (s + b) & 0xff
    return s

def inspect_coros_raw_watchface_bin(raw_bytes: bytes):
    if len(raw_bytes) < LEGACY_HEADER_BYTES:
        raise ValueError("The watch-face BIN is smaller than the 614A header.")
    if raw_bytes[0:4] != LEGACY_MAGIC:
        raise ValueError("Direct install currently accepts legacy 614A watch-face BINs only.")
    declared_payload_bytes = read_uint32_le(raw_bytes, 8)
    if declared_payload_bytes != len(raw_bytes) - LEGACY_HEADER_BYTES:
        raise ValueError("Declared payload bytes mismatch.")
    payload_crc16 = raw_bytes[12] | (raw_bytes[13] << 8)
    calc_payload = crc16_ccitt_false(raw_bytes[LEGACY_HEADER_BYTES:])
    if payload_crc16 != calc_payload:
        raise ValueError("Payload CRC mismatch.")
    watch_face_id = struct.unpack_from("<i", raw_bytes, 4)[0]
    return {
        "watchFaceId": watch_face_id,
        "sizeBytes": len(raw_bytes),
        "declaredPayloadBytes": declared_payload_bytes,
        "payloadCrc16": payload_crc16,
        "fullFileCrc16": crc16_ccitt_false(raw_bytes)
    }

def create_coros_raw_watchface_envelope(raw_bytes: bytes) -> bytes:
    envelope = bytearray(16)
    envelope[0:8] = TRANSFER_PREFIX
    write_uint32_le(envelope, 8, len(raw_bytes))
    file_crc = crc16_ccitt_false(raw_bytes)
    envelope[12] = file_crc & 0xff
    envelope[13] = (file_crc >> 8) & 0xff
    envelope_crc = crc16_ccitt_false(envelope[0:14])
    envelope[14] = envelope_crc & 0xff
    envelope[15] = (envelope_crc >> 8) & 0xff
    return bytes(envelope)

def prepare_coros_raw_watchface_transfer(raw_bytes: bytes):
    raw_copy = bytes(raw_bytes)
    bin_info = inspect_coros_raw_watchface_bin(raw_copy)
    envelope = create_coros_raw_watchface_envelope(raw_copy)
    bytes_all = envelope + raw_copy
    blocks = []
    for offset in range(0, len(bytes_all), COROS_SFT_BLOCK_SIZE):
        block_bytes = bytes_all[offset: offset + COROS_SFT_BLOCK_SIZE]
        blocks.append({
            "offset": offset,
            "remainingBytes": len(bytes_all) - offset,
            "crc16": crc16_ccitt_false(block_bytes),
            "byteSum": coros_byte_sum(block_bytes),
            "bytes": block_bytes
        })
    return {"bin": bin_info, "bytes": bytes_all, "blocks": blocks}

def create_coros_sft_start_command(block: dict) -> bytes:
    offset = block["offset"]
    if offset % 256 != 0:
        raise ValueError("Offset must be divisible by 256")
    offset_units = offset // 256
    if offset_units > 0xffffff:
        raise ValueError("Offset exceeds 24-bit field")
    length = len(block["bytes"])
    cmd = bytearray(21)
    cmd[0:4] = bytes([0x78, 0x00, COROS_SFT_DATA_INDEX, 0x00])
    cmd[4] = offset_units & 0xff
    cmd[5] = (offset_units >> 8) & 0xff
    cmd[6] = (offset_units >> 16) & 0xff
    cmd[7] = length & 0xff
    cmd[8] = (length >> 8) & 0xff
    write_uint32_le(cmd, 9, block["remainingBytes"])
    cmd[13] = block["crc16"] & 0xff
    cmd[14] = (block["crc16"] >> 8) & 0xff
    cmd[15] = block["byteSum"]
    cmd[16] = COROS_SFT_PROTOCOL_VERSION
    cmd[17] = 0x00
    cmd[18] = 0x00
    cmd[19] = 0x03
    cmd[20] = (coros_byte_sum(bytes(cmd[0:20])) ^ 0x88) & 0xff
    return bytes(cmd)

def create_coros_sft_data_windows(block: dict) -> List[List[bytes]]:
    packets = []
    packet_data = block["bytes"]
    packet_index = 0
    for offset in range(0, len(packet_data), COROS_SFT_PACKET_DATA_BYTES):
        chunk = packet_data[offset: offset + COROS_SFT_PACKET_DATA_BYTES]
        pkt = bytearray(3 + len(chunk))
        pkt[0] = 0x78
        pkt[1] = packet_index % COROS_SFT_PACKETS_PER_WINDOW
        pkt[2] = COROS_SFT_DATA_INDEX
        pkt[3:] = chunk
        packets.append(bytes(pkt))
        packet_index += 1
    windows = []
    for i in range(0, len(packets), COROS_SFT_PACKETS_PER_WINDOW):
        windows.append(packets[i:i + COROS_SFT_PACKETS_PER_WINDOW])
    return windows

def create_coros_sft_stop_command() -> bytes:
    return bytes([0x78, 0x08, 0x00, 0x00, 0x01])

# ---- Public host upload and QR generation ----

def upload_transfer_sh(filepath: str) -> str:
    url = f"https://transfer.sh/{os.path.basename(filepath)}"
    with open(filepath, "rb") as f:
        resp = requests.put(url, data=f)
    resp.raise_for_status()
    return resp.text.strip()

def upload_anonfiles(filepath: str) -> str:
    upurl = "https://api.anonfiles.com/upload"
    with open(filepath, "rb") as f:
        files = {"file": (os.path.basename(filepath), f)}
        resp = requests.post(upurl, files=files)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("status"):
        raise RuntimeError("anonfiles upload failed")
    return data["data"]["file"]["url"]["full"]

def generate_qr(url: str, outpath: str):
    if qrcode is None:
        raise RuntimeError("qrcode not installed. pip install qrcode[pil]")
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_Q)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(outpath)
    return outpath

# ---- USB copy utility ----

def usb_copy(file_path: str, mount_path: str) -> str:
    if not os.path.isdir(mount_path):
        raise FileNotFoundError("Mount path not found")
    dest = os.path.join(mount_path, os.path.basename(file_path))
    shutil.copy2(file_path, dest)
    return dest

# ---- BLE transfer (async) ----

# Service constants copied from CorosLink
COROS_CONTROL_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-77656c6f6f70"
COROS_BULK_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
COROS_AUX_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-77757c7f7f70"

async def ble_transfer(file_path: str, device_address: str = None, char_uuids: dict = None):
    if BleakClient is None:
        raise RuntimeError("bleak is not installed. pip install bleak")
    bcontent = open(file_path, "rb").read()
    transfer = prepare_coros_raw_watchface_transfer(bcontent)
    # connect
    if device_address is None:
        devices = await BleakScanner.discover(timeout=5.0)
        if not devices:
            raise RuntimeError("No BLE devices found; pass --device-address")
        device = devices[0]
        address = device.address
    else:
        address = device_address

    async with BleakClient(address) as client:
        if not client.is_connected:
            raise RuntimeError("Could not connect to BLE device")

        svcs = await client.get_services()

        def find_char(svc_uuid, require_notify=False, require_write=False, require_write_without_response=False):
            svc = svcs.get_service(svc_uuid)
            if not svc:
                return None
            for ch in svc.characteristics:
                props = ch.properties
                if require_notify and "notify" in props:
                    return ch.uuid
                if require_write and "write" in props:
                    return ch.uuid
                if require_write_without_response and ("write-without-response" in props or "write_without_response" in props):
                    return ch.uuid
            return None

        control_write = (char_uuids or {}).get("control_write") or find_char(COROS_CONTROL_SERVICE_UUID, require_write=True)
        control_notify = (char_uuids or {}).get("control_notify") or find_char(COROS_CONTROL_SERVICE_UUID, require_notify=True)
        bulk_write = (char_uuids or {}).get("bulk_write") or (find_char(COROS_BULK_SERVICE_UUID, require_write_without_response=True) or find_char(COROS_BULK_SERVICE_UUID, require_write=True))
        bulk_notify = (char_uuids or {}).get("bulk_notify") or find_char(COROS_BULK_SERVICE_UUID, require_notify=True)
        aux_notify = (char_uuids or {}).get("aux_notify") or find_char(COROS_AUX_SERVICE_UUID, require_notify=True)

        if not (control_write and control_notify and bulk_write and bulk_notify and aux_notify):
            raise RuntimeError("Could not resolve required COROS characteristics automatically. Provide explicit characteristic UUIDs from CorosLink constants.")

        control_queue = asyncio.Queue()
        bulk_queue = asyncio.Queue()

        def control_handler(sender, data):
            control_queue.put_nowait(bytes(data))
        def bulk_handler(sender, data):
            bulk_queue.put_nowait(bytes(data))

        await client.start_notify(control_notify, control_handler)
        await client.start_notify(bulk_notify, bulk_handler)
        await client.start_notify(aux_notify, lambda s, d: None)

        async def write_no_response(uuid_bytes: str, data: bytes):
            await client.write_gatt_char(uuid_bytes, data, response=False)

        async def wait_for_queue(q: asyncio.Queue, predicate, timeout, purpose):
            try:
                while True:
                    item = await asyncio.wait_for(q.get(), timeout=timeout)
                    if predicate(item):
                        return item
            except asyncio.TimeoutError:
                raise TimeoutError(f"Timed out waiting for {purpose}")

        for block_idx, block in enumerate(transfer["blocks"]):
            start_cmd = create_coros_sft_start_command(block)
            ready = False
            for attempt in range(2):
                await write_no_response(control_write, start_cmd)
                try:
                    def predicate(v: bytes):
                        return v.startswith(b"\x78\x00")
                    resp = await wait_for_queue(control_queue, predicate, 8.0, "the watch to accept the next SFT block")
                except TimeoutError:
                    if block_idx == 0:
                        raise RuntimeError("The watch did not acknowledge SFT. It may require a SystemBind authentication session.")
                    raise
                # best-effort acceptance check
                ready = True
                break
            if not ready:
                raise RuntimeError("The watch rejected the SFT block start request.")

            windows = create_coros_sft_data_windows(block)
            for win_idx, window in enumerate(windows):
                for packet in window:
                    await write_no_response(bulk_write, packet)
                    await asyncio.sleep(0.001)
                if win_idx < len(windows) - 1:
                    def pred_bulk(v: bytes):
                        return v.startswith(b"\x78\x00")
                    await wait_for_queue(bulk_queue, pred_bulk, 8.0, "the watch to acknowledge the SFT packet window")

            def pred_verify(v: bytes):
                return v.startswith(bytes([0x78, 0x00, 0x01]))
            await wait_for_queue(control_queue, pred_verify, 8.0, "the watch to verify the SFT block")

            completed_bytes = block["offset"] + len(block["bytes"])
            print(f"Block {block_idx+1}/{len(transfer['blocks'])} sent ({completed_bytes} / {len(transfer['bytes'])})")

        stop_cmd = create_coros_sft_stop_command()
        await write_no_response(control_write, stop_cmd)

        print("Transfer completed (best-effort).")
        await asyncio.sleep(0.5)
        await client.stop_notify(control_notify)
        await client.stop_notify(bulk_notify)
        await client.stop_notify(aux_notify)

# ---- CLI ----

def main():
    parser = argparse.ArgumentParser(description="Upload COROS watchface and optionally generate QR / transfer via USB or BLE.")
    parser.add_argument("--file", "-f", required=True, help="Path to watch face file")
    parser.add_argument("--mode", "-m", choices=["host", "usb", "ble"], default="host")
    parser.add_argument("--host", choices=["transfer.sh", "anonfiles"], default="transfer.sh")
    parser.add_argument("--qr", help="Path to save QR code PNG (mode=host).")
    parser.add_argument("--mount", help="Mount path for USB mode.")
    parser.add_argument("--device-address", help="BLE device address for BLE mode (optional).")
    parser.add_argument("--char-control-write", help="Explicit COROS control write characteristic UUID")
    parser.add_argument("--char-control-notify", help="Explicit COROS control notify characteristic UUID")
    parser.add_argument("--char-bulk-write", help="Explicit COROS bulk write characteristic UUID")
    parser.add_argument("--char-bulk-notify", help="Explicit COROS bulk notify characteristic UUID")
    parser.add_argument("--char-aux-notify", help="Explicit COROS aux notify characteristic UUID")
    args = parser.parse_args()

    fp = args.file
    if not os.path.isfile(fp):
        print("File not found", fp, file=sys.stderr)
        sys.exit(2)

    if args.mode == "host":
        if args.host == "transfer.sh":
            url = upload_transfer_sh(fp)
        else:
            url = upload_anonfiles(fp)
        print("Uploaded URL:", url)
        if args.qr:
            out = generate_qr(url, args.qr)
            print("Saved QR to", out)
        return

    if args.mode == "usb":
        if not args.mount:
            print("--mount is required for usb mode", file=sys.stderr)
            sys.exit(2)
        dest = usb_copy(fp, args.mount)
        print("Copied to", dest)
        return

    if args.mode == "ble":
        if BleakClient is None:
            print("bleak required for BLE mode. pip install bleak", file=sys.stderr)
            sys.exit(2)
        charmap = {}
        if args.char_control_write: charmap["control_write"] = args.char_control_write
        if args.char_control_notify: charmap["control_notify"] = args.char_control_notify
        if args.char_bulk_write: charmap["bulk_write"] = args.char_bulk_write
        if args.char_bulk_notify: charmap["bulk_notify"] = args.char_bulk_notify
        if args.char_aux_notify: charmap["aux_notify"] = args.char_aux_notify
        asyncio.run(ble_transfer(fp, device_address=args.device_address, char_uuids=charmap))
        return

if __name__ == "__main__":
    main()
