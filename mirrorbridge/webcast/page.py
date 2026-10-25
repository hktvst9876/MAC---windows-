"""The web page a browser opens to cast its screen."""

import html

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cast to %(name)s</title>
<style>
  :root { color-scheme: light dark; --accent: #2f80ed; }
  body { margin: 0; font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         background: Canvas; color: CanvasText; }
  header { background: linear-gradient(135deg, #1f5fd1, #4facfe); color: white; padding: 28px 24px; }
  header h1 { margin: 0; font-size: 24px; }
  header p { margin: 6px 0 0; opacity: .9; }
  main { max-width: 560px; margin: 0 auto; padding: 24px; }
  label { display: block; margin: 14px 0 6px; font-weight: 600; }
  input[type=text], input[type=password], select { width: 100%%; box-sizing: border-box; padding: 10px;
         border-radius: 8px; border: 1px solid #8886; font-size: 15px; background: Field; color: FieldText; }
  .check { font-weight: 400; display: flex; gap: 8px; align-items: center; }
  button { margin-top: 22px; width: 100%%; padding: 14px; font-size: 17px; font-weight: 600; border: 0;
           border-radius: 10px; background: var(--accent); color: white; cursor: pointer; }
  button.stop { background: #e5484d; }
  button:disabled { opacity: .5; cursor: default; }
  #status { margin-top: 18px; min-height: 1.4em; }
  .live { color: #27ae60; font-weight: 600; }
  .warn { color: #e5484d; }
  video { width: 100%%; margin-top: 16px; border-radius: 10px; background: #000; }
  small { opacity: .7; }
</style>
</head>
<body>
<header>
  <h1>Cast your screen to %(name)s</h1>
  <p>Works in Chrome, Edge and Firefox on Chromebooks and computers.</p>
</header>
<main>
  <label for="devname">Your device name</label>
  <input id="devname" type="text" maxlength="40">
  <div id="pwrow" hidden>
    <label for="pw">Password</label>
    <input id="pw" type="password">
  </div>
  <label for="quality">Quality</label>
  <select id="quality">
    <option value="720">720p (smoother on slow Wi-Fi)</option>
    <option value="1080" selected>1080p</option>
  </select>
  <label class="check"><input id="audio" type="checkbox" checked> Share audio too (if your browser offers it)</label>
  <button id="start">Start casting</button>
  <button id="stop" class="stop" hidden>Stop casting</button>
  <div id="status"></div>
  <video id="preview" muted playsinline autoplay hidden></video>
  <p><small>Tip: choose "Entire screen" to mirror everything, or a single tab or window.</small></p>
</main>
<script>
const NEEDS_PASSWORD = %(password)s;
const $ = (id) => document.getElementById(id);
const supported = !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia && window.MediaRecorder);
let ws = null, recorder = null, stream = null;

function guessName() {
  const ua = navigator.userAgent;
  if (/CrOS/.test(ua)) return "Chromebook";
  if (/Android/.test(ua)) return "Android device";
  if (/Windows/.test(ua)) return "Windows PC";
  if (/Mac OS X/.test(ua)) return "Mac";
  if (/Linux/.test(ua)) return "Linux PC";
  return "Web browser";
}
function setStatus(text, cls) { const s = $("status"); s.textContent = text; s.className = cls || ""; }
function pickMime(withAudio) {
  const video = ["h264", "vp8", "vp9"];
  const list = [];
  for (const v of video) { if (withAudio) list.push(`video/webm;codecs=${v},opus`); list.push(`video/webm;codecs=${v}`); }
  list.push("video/webm", "video/mp4");
  return list.find((m) => MediaRecorder.isTypeSupported(m)) || "";
}

try { $("devname").value = localStorage.getItem("mb-name") || guessName(); } catch (e) { $("devname").value = guessName(); }
if (NEEDS_PASSWORD) $("pwrow").hidden = false;
if (!supported) {
  $("start").disabled = true;
  setStatus("This browser cannot share its screen. Please use Chrome or Edge on a computer or Chromebook " +
            "(phones and tablets do not allow screen sharing from a web page).", "warn");
}

function stop(message) {
  if (recorder && recorder.state !== "inactive") { try { recorder.stop(); } catch (e) {} }
  recorder = null;
  if (stream) stream.getTracks().forEach((t) => t.stop());
  stream = null;
  if (ws) { const w = ws; ws = null; try { w.close(); } catch (e) {} }
  $("preview").hidden = true;
  $("start").hidden = false; $("stop").hidden = true; $("start").disabled = !supported;
  if (message) setStatus(message, message.startsWith("Casting stopped") ? "" : "warn");
}

$("stop").onclick = () => stop("Casting stopped.");

$("start").onclick = async () => {
  try { localStorage.setItem("mb-name", $("devname").value); } catch (e) {}
  $("start").disabled = true;
  const height = parseInt($("quality").value, 10);
  try {
    stream = await navigator.mediaDevices.getDisplayMedia({
      video: { frameRate: { ideal: 30, max: 30 }, height: { ideal: height } },
      audio: $("audio").checked,
    });
  } catch (e) {
    $("start").disabled = false;
    setStatus(e && e.name === "NotAllowedError" ? "Screen sharing was cancelled." :
              "Screen sharing failed: " + (e && (e.message || e.name)), "warn");
    return;
  }
  const mime = pickMime(stream.getAudioTracks().length > 0);
  setStatus("Connecting…");
  ws = new WebSocket(`wss://${location.host}/ws`);
  ws.onopen = () => ws.send(JSON.stringify({
    type: "hello", name: $("devname").value, password: $("pw").value, mime: mime, ua: navigator.userAgent,
  }));
  ws.onmessage = (ev) => {
    let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.type === "ready") start(mime, height);
    else if (msg.type === "error") stop(msg.message);
    else if (msg.type === "bye") stop("Casting stopped by the receiver.");
  };
  ws.onclose = () => { if (ws) stop("Connection to the receiver was lost."); };
  stream.getVideoTracks()[0].addEventListener("ended", () => stop("Casting stopped."));
};

function start(mime, height) {
  const options = { videoBitsPerSecond: height >= 1080 ? 8000000 : 5000000, audioBitsPerSecond: 128000 };
  if (mime) options.mimeType = mime;
  recorder = new MediaRecorder(stream, options);
  recorder.ondataavailable = (e) => { if (e.data.size && ws && ws.readyState === 1) ws.send(e.data); };
  recorder.start(100);
  const preview = $("preview");
  preview.srcObject = stream; preview.hidden = false;
  $("start").hidden = true; $("stop").hidden = false;
  setStatus("● Casting to %(name)s", "live");
}
</script>
</body>
</html>
"""


def render(receiver_name: str, needs_password: bool) -> bytes:
    name = html.escape(receiver_name)
    return (PAGE % {"name": name, "password": "true" if needs_password else "false"}).encode("utf-8")
