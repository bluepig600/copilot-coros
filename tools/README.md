# tools/README.md

This folder contains tools to upload and publish COROS watchfaces and to perform raw BLE installs.

Quick start

1. Install dependencies

   python -m pip install -r tools/requirements.txt

2. Run the local webapp (Flask)

   python tools/run_webapp.py

   Open http://localhost:8080 in your browser.

Watch face editor (simple)

- The web app includes a simple watch face editor section inspired by CorosLink.
- Upload a background image, set text/position/color, and click **Save PNG to server**.
- Saved files are written to `tools/uploads/` and exposed at `/uploads/<filename>`.
- You can then upload the saved PNG using the existing upload form/workflow.

CLI usage

- Upload and generate QR (transfer.sh):

  python tools/coros_uploader.py --file myface.zip --mode host --host transfer.sh --qr qr.png

- Copy to USB mount:

  python tools/coros_uploader.py --file myface.dat --mode usb --mount /media/watch

- BLE raw install (requires bleak and BLE adapter):

  python tools/coros_uploader.py --file legacy614a.bin --mode ble --device-address AA:BB:CC:DD:EE:FF

Publish to COROS (interactive)

- Use tools/coros_publish.py functions from a Python REPL or integrate with the webapp. The publish flow will prompt for credentials and requires you to supply your COROS account details interactively.

Security & legal

- The publish flow uses COROS mobile endpoints captured by CorosLink. These are undocumented private APIs — use only with accounts and watchfaces you own or have permission to publish.
- The webapp stores uploaded files in tools/uploads/ locally; do not expose this webapp to untrusted networks.
