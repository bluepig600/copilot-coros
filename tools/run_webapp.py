"""
Local Flask webapp to upload/publish COROS watchfaces and initiate BLE/USB installs.

Run: python tools/run_webapp.py
Visit: http://localhost:8080
"""

import os
import threading
import asyncio
import base64
import binascii
from flask import Flask, request, render_template_string, send_file, url_for
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
<hr>
<h2>Simple Watch Face Editor</h2>
<p>Inspired by CorosLink: upload a background, overlay text, and save a PNG.</p>
<div style="display:flex; gap:20px; flex-wrap:wrap; align-items:flex-start;">
  <div>
    <canvas id="wf-canvas" width="400" height="400" style="border:1px solid #999; background:#000"></canvas>
  </div>
  <div>
    <p>Background image: <input type="file" id="wf-bg" accept="image/*"></p>
    <p>Text: <input id="wf-text" value="12:45"></p>
    <p>Text color: <input type="color" id="wf-color" value="#ffffff"></p>
    <p>Font size: <input type="range" id="wf-size" min="12" max="180" value="96"></p>
    <p>X position: <input type="range" id="wf-x" min="0" max="400" value="200"></p>
    <p>Y position: <input type="range" id="wf-y" min="0" max="400" value="200"></p>
    <p>Background color: <input type="color" id="wf-bgcolor" value="#000000"></p>
    <p><button type="button" id="wf-save">Save PNG to server</button></p>
    <p id="wf-result"></p>
  </div>
</div>
<script>
(() => {
  const canvas = document.getElementById("wf-canvas");
  const ctx = canvas.getContext("2d");
  const bgInput = document.getElementById("wf-bg");
  const textInput = document.getElementById("wf-text");
  const colorInput = document.getElementById("wf-color");
  const sizeInput = document.getElementById("wf-size");
  const xInput = document.getElementById("wf-x");
  const yInput = document.getElementById("wf-y");
  const bgColorInput = document.getElementById("wf-bgcolor");
  const saveButton = document.getElementById("wf-save");
  const result = document.getElementById("wf-result");
  let bgImage = null;

  function draw() {
    ctx.fillStyle = bgColorInput.value || "#000000";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    if (bgImage) {
      const scale = Math.max(canvas.width / bgImage.width, canvas.height / bgImage.height);
      const w = bgImage.width * scale;
      const h = bgImage.height * scale;
      ctx.drawImage(bgImage, (canvas.width - w) / 2, (canvas.height - h) / 2, w, h);
    }
    ctx.fillStyle = colorInput.value || "#ffffff";
    ctx.font = `700 ${sizeInput.value}px sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(textInput.value || "", Number(xInput.value), Number(yInput.value));
  }

  function loadBackground(file) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const img = new Image();
      img.onload = () => {
        bgImage = img;
        draw();
      };
      img.src = String(reader.result || "");
    };
    reader.readAsDataURL(file);
  }

  [textInput, colorInput, sizeInput, xInput, yInput, bgColorInput].forEach((el) => {
    el.addEventListener("input", draw);
  });
  bgInput.addEventListener("change", () => loadBackground(bgInput.files && bgInput.files[0]));

  saveButton.addEventListener("click", async () => {
    const dataUrl = canvas.toDataURL("image/png");
    result.textContent = "Saving...";
    try {
      const resp = await fetch("/editor/save", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({data_url: dataUrl, filename: "watchface_editor.png"})
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "Save failed");
      result.innerHTML = `Saved: <a href="${data.url}" target="_blank">${data.filename}</a>`;
    } catch (err) {
      result.textContent = "Save failed: " + (err && err.message ? err.message : String(err));
    }
  });

  draw();
})();
</script>
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

@app.route('/editor/save', methods=['POST'])
def editor_save():
    payload = request.get_json(silent=True) or {}
    data_url = payload.get("data_url") or ""
    filename = secure_filename(payload.get("filename") or "watchface_editor.png")
    if not filename:
        filename = "watchface_editor.png"
    if not filename.lower().endswith(".png"):
        filename = f"{filename}.png"
    if not data_url.startswith("data:image/png;base64,"):
        return {"error": "Only PNG data URLs are supported."}, 400
    encoded = data_url.split(",", 1)[1] if "," in data_url else ""
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return {"error": "Invalid image payload."}, 400
    out_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    with open(out_path, "wb") as f:
        f.write(image_bytes)
    return {"filename": filename, "url": url_for("uploaded_file", filename=filename)}

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    path = os.path.join(app.config["UPLOAD_FOLDER"], secure_filename(filename))
    if not os.path.exists(path):
        return "File not found", 404
    return send_file(path, mimetype="image/png")

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8080)
