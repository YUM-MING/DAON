/* =========================================================
   robot-sensors.js — 로봇 폰의 카메라·마이크를 서버로 스트리밍 (통합자 추가분)

   - 카메라: 전면 카메라 → 320x240 JPEG(품질 0.6) → 초당 fps 장 → WebSocket 바이너리 [0x01 + JPEG]
   - 마이크: 16kHz 모노 Int16 PCM → 약 0.13초마다 → WebSocket 바이너리 [0x02 + PCM]
   - 서버의 비전(유민)·음성(정연) 워커가 이걸 받아 낙상/표정/낙상음/STT 를 판정한다.
   - getUserMedia 는 https(또는 localhost)에서만 동작 → 밖에서는 ./run.sh --tunnel 주소로 접속.
   - 브라우저 정책상 첫 사용자 터치가 필요할 수 있어 "카메라·마이크 켜기" 버튼을 띄운다.
========================================================= */
(function(){
  if(!document.getElementById("face") || location.protocol === "file:") return;

  let stream = null, video = null, canvas = null, ctx2d = null, frameTimer = null, audioCtx = null;
  let fps = 8, sending = { cam:false, mic:false }, frames = 0, pcmChunks = 0;
  const btn = document.createElement("button");
  btn.className = "btn danger";
  btn.id = "sensorStart";
  btn.textContent = "📷🎤 카메라·마이크 켜기";
  Object.assign(btn.style, { position:"fixed", left:"50%", bottom:"12vh", transform:"translateX(-50%)", zIndex:300, display:"none" });
  document.body.appendChild(btn);
  const ind = document.createElement("div");
  ind.id = "sensorInd";
  Object.assign(ind.style, { position:"fixed", left:"14px", bottom:"8px", fontSize:"10px", color:"#4a5568", zIndex:30 });
  document.body.appendChild(ind);
  const setInd = () => { ind.textContent = `📷 ${sending.cam ? frames + "f" : "off"} · 🎤 ${sending.mic ? pcmChunks + "c" : "off"}`; };
  const say = (m) => { if(typeof log === "function") log(m); };

  async function start(){
    if(stream) return;
    if(!window.isSecureContext){
      say("카메라·마이크는 https 주소에서만 켜집니다 (./run.sh --tunnel)");
      if(typeof toast === "function") toast("https 주소로 접속해야 카메라·마이크를 켤 수 있어요");
      return;
    }
    try{
      stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "user", width: { ideal: 640 }, height: { ideal: 480 }, frameRate: { ideal: 15 } },
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    }catch(e){
      say("카메라·마이크 권한 실패: " + e.name);
      btn.style.display = "block";
      btn.textContent = "📷🎤 카메라·마이크 켜기 (" + e.name + ")";
      return;
    }
    btn.style.display = "none";
    startCamera(); startMic();
    say("카메라·마이크 스트리밍 시작");
  }

  function startCamera(){
    video = document.createElement("video");
    video.setAttribute("playsinline", ""); video.muted = true; video.autoplay = true;
    Object.assign(video.style, { position:"fixed", width:"1px", height:"1px", opacity:0, pointerEvents:"none" });
    video.srcObject = stream; document.body.appendChild(video);
    video.play().catch(()=>{});
    canvas = document.createElement("canvas"); canvas.width = 320; canvas.height = 240;
    ctx2d = canvas.getContext("2d", { willReadFrequently: false });
    sending.cam = true;
    frameTimer = setInterval(captureFrame, 1000 / fps);
  }

  function captureFrame(){
    if(!window.DaonWS || !DaonWS.connected() || !video || video.readyState < 2) return;
    ctx2d.drawImage(video, 0, 0, canvas.width, canvas.height);
    canvas.toBlob(blob => {
      if(!blob) return;
      blob.arrayBuffer().then(ab => {
        const out = new Uint8Array(ab.byteLength + 1); out[0] = 1; out.set(new Uint8Array(ab), 1);
        DaonWS.sendBinary(out.buffer); frames++; if(frames % 10 === 0) setInd();
      });
    }, "image/jpeg", 0.6);
  }

  function startMic(){
    try{ audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 }); }
    catch(e){ audioCtx = new (window.AudioContext || window.webkitAudioContext)(); }
    const sr = audioCtx.sampleRate, ratio = sr / 16000;
    const src = audioCtx.createMediaStreamSource(stream);
    const proc = audioCtx.createScriptProcessor(2048, 1, 1);
    const sink = audioCtx.createGain(); sink.gain.value = 0;          // 마이크 소리를 스피커로 내보내지 않는다
    proc.onaudioprocess = (e) => {
      if(!window.DaonWS || !DaonWS.connected()) return;
      const f = e.inputBuffer.getChannelData(0);
      let out;
      if(ratio <= 1.01){ out = f; }
      else {                                                           // 48kHz 등 → 16kHz 로 평균 다운샘플
        const n = Math.floor(f.length / ratio); out = new Float32Array(n);
        for(let i=0;i<n;i++){ const a = Math.floor(i*ratio), b = Math.floor((i+1)*ratio); let s=0; for(let j=a;j<b;j++) s += f[j]; out[i] = s / Math.max(1, b-a); }
      }
      const pcm = new Int16Array(out.length);
      for(let i=0;i<out.length;i++){ const v = Math.max(-1, Math.min(1, out[i])); pcm[i] = v < 0 ? v * 32768 : v * 32767; }
      const buf = new Uint8Array(pcm.byteLength + 1); buf[0] = 2; buf.set(new Uint8Array(pcm.buffer), 1);
      DaonWS.sendBinary(buf.buffer); pcmChunks++; if(pcmChunks % 20 === 0) setInd();
    };
    src.connect(proc); proc.connect(sink); sink.connect(audioCtx.destination);
    sending.mic = true; setInd();
    if(audioCtx.state === "suspended") audioCtx.resume().catch(()=>{});
  }

  btn.addEventListener("click", start);
  document.addEventListener("daon:conn", (e) => { if(e.detail.ok && !stream) start(); });
  if(window.DaonReady) DaonReady.then(info => { if(info && info.fps) fps = Math.min(15, Math.max(2, info.fps)); start(); }).catch(()=>{});
  // 폰 브라우저가 터치 없이 권한을 안 주는 경우: 첫 터치에서 다시 시도
  document.addEventListener("touchstart", () => { if(!stream) start(); }, { once: true, passive: true });
  document.addEventListener("click", () => { if(!stream) start(); }, { once: true });
  window.DaonSensors = { start, status: () => ({ ...sending, frames, pcmChunks, fps }) };
})();
