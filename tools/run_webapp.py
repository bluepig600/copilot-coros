"""
Local Flask webapp to upload/publish COROS watchfaces and initiate BLE/USB installs.

Run: python tools/run_webapp.py
Visit: http://localhost:8080
"""

import os
import threading
import asyncio
from flask import Flask, request, render_template_string, send_file, redirect, url_for
from werkzeug.utils import secure_filename

from coros_uploader import upload_transfer_sh, upload_anonfiles, generate_qr, usb_copy, ble_transfer
from coros_publish import publish_coros_watchface, mobile_request

UPLOAD_DIR = os.path.join(os.getcwd(), "tools", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_DIR

INDEX_HTML = """<!doctype html>
<title>COROS Tools</title>
<h1>COROS Tools - Upload / Publish / BLE</h1>
<form method=post enctype=multipart/form-data action="/upload">
  <p><input type=file name=file>
  <p>Mode: <select name=mode>
    <option value=host>host (transfer.sh)</option>
    <option value=publish>publish (COROS mobile)</option>
    <option value=usb>usb</option>
    <option value=ble>ble</option>
  </select>
  <p>Mount path (for USB): <input name=mount>
  <p>BLE device address: <input name=device_address>
  <p>Background image id (publish): <input name=background_image_id value=0>
  <p>Firmware type (publish): <input name=firmware_type value="COROS W332">
  <p>Name (publish): <input name=name value="My COROS Face">
  <p><input type=submit value=Upload>
</form>
"""

@app.route('/')
def index():
    return render_template_string(INDEX_HTML)

@app.route('/upload', methods=['POST'])
def upload():
    f = request.files.get('file')
    if not f:
        return "No file uploaded", 400
    filename = secure_filename(f.filename)
    path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    f.save(path)
    mode = request.form.get('mode')
    if mode == 'host':
        url = upload_transfer_sh(path)
        qr_path = os.path.join(app.config['UPLOAD_FOLDER'], 'qr.png')
        generate_qr(url, qr_path)
        return f"Uploaded: <a href=\"{url}\">{url}</a><br><img src=\"/qr\">"
    if mode == 'usb':
        mount = request.form.get('mount')
        if not mount:
            return "Mount required", 400
        dest = usb_copy(path, mount)
        return f"Copied to {dest}"
    if mode == 'ble':
        addr = request.form.get('device_address')
        # run BLE transfer in background thread to avoid blocking flask
        def run_ble():
            asyncio.run(ble_transfer(path, device_address=addr))
        t = threading.Thread(target=run_ble, daemon=True)
        t.start()
        return "BLE transfer started; watch console for progress"
    if mode == 'publish':
        # interactive: ask for credentials and then publish
        name = request.form.get('name')
        firmware = request.form.get('firmware_type')
        bgid = int(request.form.get('background_image_id') or 0)
        # For demo, prompt via simple form redirect
        return render_template_string('''<form method=post action="/do_publish">
            Email: <input name=email><br>
            Password: <input name=password type=password><br>
            Region (us/eu/cn): <input name=region value=us><br>
            <input type=hidden name=archive_path value="%s">
            <input type=hidden name=name value="%s">
            <input type=hidden name=firmware value="%s">
            <input type=hidden name=bgid value="%s">
            <input type=submit value="Publish">
        </form>''' % (path, name, firmware, bgid))
    return "Unknown mode", 400

@app.route('/do_publish', methods=['POST'])
def do_publish():
    email = request.form.get('email')
    password = request.form.get('password')
    region = request.form.get('region')
    archive_path = request.form.get('archive_path')
    name = request.form.get('name')
    firmware = request.form.get('firmware')
    bgid = int(request.form.get('bgid') or 0)
    # For simplicity reuse mobile_request to login here is omitted; this is a demo placeholder
    return "Publish via coros_publish module is available in tools/coros_publish.py — run interactively for credential entry."

@app.route('/qr')
def qr():
    qr_path = os.path.join(app.config['UPLOAD_FOLDER'], 'qr.png')
    if not os.path.exists(qr_path):
        return "No QR", 404
    return send_file(qr_path, mimetype='image/png')

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8080)
