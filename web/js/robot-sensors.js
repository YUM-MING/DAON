/* =========================================================
   robot-sensors.js — 로봇 폰의 카메라·마이크를 서버로 스트리밍 (통합자 추가분)

   - 카메라: 전면 카메라 → 320x240 JPEG(품질 0.6) → 초당 fps 장 → WebSocket 바이너리 [0x01 + JPEG]
   - 마이크: 16kHz 모노 Int16 PCM → WebSocket 바이너리 [0x02 + PCM]
     AudioWorklet(오디오 스레드)로 캡처한다. ScriptProcessor(메인 스레드)는 JPEG 인코딩과 겹쳐 버퍼가 끊기고
     클릭 잡음이 생겨(2026-10-18 녹음에서 확인) 분류기·STT 를 망친다. 지원 안 되는 브라우저만 ScriptProcessor 로.
   - 자동 볼륨(AGC)은 끈다: 클리핑(피크 1.0)과 펌핑이 whisper 인식률을 떨어뜨렸다.
     URL 에 ?raw=1 을 붙이면 에코 제거·노이즈 억제까지 전부 끄고 원음을 보낸다 (실험용).
   - getUserMedia 는 https(또는 localhost)에서만 동작 → 밖에서는 ./run.sh --tunnel 주소로 접속.
========================================================= */
(function(){
  if(!document.getElementById("face") || location.protocol === "file:") return;

  const params = new URLSearchParams(location.search);
  const RAW = params.get("raw") === "1";
  let stream = null, video = null, canvas = null, ctx2d = null, frameTimer = null, audioCtx = null;
  let fps = 6, sending = { cam:false, mic:false }, frames = 0, pcmChunks = 0, zeroChunks = 0;
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
  const setInd = () => { ind.textContent = `📷 ${sending.cam ? frames + "f" : "off"} · 🎤 ${sending.mic ? pcmChunks + "c" + (zeroChunks ? " (무음 " + zeroChunks + ")" : "") : "off"}${RAW ? " · raw" : ""}`; };
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
        audio: RAW ? { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false }
                   : { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: false },
      });
    }catch(e){
      say("카메라·마이크 권한 실패: " + e.name);
      btn.style.display = "block";
      btn.textContent = "📷🎤 카메라·마이크 켜기 (" + e.name + ")";
      return;
    }
    btn.style.display = "none";
    const track = stream.getAudioTracks()[0];
    if(track){
      say("마이크: " + track.label + " " + JSON.stringify(track.getSettings ? { sr: track.getSettings().sampleRate, aec: track.getSettings().echoCancellation, agc: track.getSettings().autoGainControl } : {}));
      track.onmute = () => say("⚠ 마이크 트랙 mute 됨 (OS/브라우저가 소리를 막음)");
      track.onunmute = () => say("마이크 트랙 unmute");
    }
    startCamera();
    await startMic();
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

  function sendPcm(int16){
    if(!window.DaonWS || !DaonWS.connected()) return;
    let z = 0; for(let i = 0; i < int16.length; i += 16) if(int16[i] === 0) z++;
    if(z * 16 >= int16.length * 0.9) zeroChunks++;
    const buf = new Uint8Array(int16.byteLength + 1); buf[0] = 2; buf.set(new Uint8Array(int16.buffer, int16.byteOffset, int16.byteLength), 1);
    DaonWS.sendBinary(buf.buffer); pcmChunks++; if(pcmChunks % 20 === 0) setInd();
  }

  /* AudioWorklet 프로세서: 입력을 16kHz 로 평균 다운샘플해 2048 샘플(128ms) Int16 블록으로 메인 스레드에 보낸다 */
  const WORKLET_SRC = `
    class PcmSender extends AudioWorkletProcessor {
      constructor(){ super(); this.ratio = sampleRate / 16000; this.acc = 0; this.n = 0; this.out = new Int16Array(2048); this.k = 0; this.phase = 0; }
      process(inputs){
        const ch = inputs[0] && inputs[0][0]; if(!ch) return true;
        for(let i = 0; i < ch.length; i++){
          this.acc += ch[i]; this.n++; this.phase += 1;
          if(this.phase >= this.ratio){
            this.phase -= this.ratio;
            const v = Math.max(-1, Math.min(1, this.acc / this.n)); this.acc = 0; this.n = 0;
            this.out[this.k++] = v < 0 ? v * 32768 : v * 32767;
            if(this.k === 2048){ this.port.postMessage(this.out, [this.out.buffer]); this.out = new Int16Array(2048); this.k = 0; }
          }
        }
        return true;
      }
    }
    registerProcessor("pcm-sender", PcmSender);`;

  async function startMic(){
    try{ audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 }); }
    catch(e){ audioCtx = new (window.AudioContext || window.webkitAudioContext)(); }
    if(audioCtx.state === "suspended") await audioCtx.resume().catch(()=>{});
    const src = audioCtx.createMediaStreamSource(stream);
    const sink = audioCtx.createGain(); sink.gain.value = 0;          // 마이크 소리를 스피커로 내보내지 않는다
    say(`오디오 컨텍스트 ${audioCtx.sampleRate}Hz`);
    if(audioCtx.audioWorklet){
      try{
        const url = URL.createObjectURL(new Blob([WORKLET_SRC], { type: "application/javascript" }));
        await audioCtx.audioWorklet.addModule(url);
        const node = new AudioWorkletNode(audioCtx, "pcm-sender");
        node.port.onmessage = (e) => sendPcm(new Int16Array(e.data));
        src.connect(node); node.connect(sink); sink.connect(audioCtx.destination);
        sending.mic = true; setInd(); say("마이크 캡처: AudioWorklet");
        return;
      }catch(e){ say("AudioWorklet 실패, ScriptProcessor 로 대체: " + e.message); }
    }
    const sr = audioCtx.sampleRate, ratio = sr / 16000;
    const proc = audioCtx.createScriptProcessor(2048, 1, 1);
    proc.onaudioprocess = (e) => {
      const f = e.inputBuffer.getChannelData(0);
      let out;
      if(ratio <= 1.01){ out = f; }
      else { const n = Math.floor(f.length / ratio); out = new Float32Array(n);
        for(let i=0;i<n;i++){ const a = Math.floor(i*ratio), b = Math.floor((i+1)*ratio); let s=0; for(let j=a;j<b;j++) s += f[j]; out[i] = s / Math.max(1, b-a); } }
      const pcm = new Int16Array(out.length);
      for(let i=0;i<out.length;i++){ const v = Math.max(-1, Math.min(1, out[i])); pcm[i] = v < 0 ? v * 32768 : v * 32767; }
      sendPcm(pcm);
    };
    src.connect(proc); proc.connect(sink); sink.connect(audioCtx.destination);
    sending.mic = true; setInd(); say("마이크 캡처: ScriptProcessor");
  }

  // 화면이 꺼지거나 탭이 뒤로 가면 오디오 컨텍스트가 멈춘다 → 다시 보이면 재개
  document.addEventListener("visibilitychange", () => { if(!document.hidden && audioCtx && audioCtx.state === "suspended") audioCtx.resume().catch(()=>{}); });

  btn.addEventListener("click", start);
  document.addEventListener("daon:conn", (e) => { if(e.detail.ok && !stream) start(); });
  if(window.DaonReady) DaonReady.then(info => { if(info && info.fps) fps = Math.min(15, Math.max(2, info.fps)); start(); }).catch(()=>{});
  document.addEventListener("touchstart", () => { if(!stream) start(); }, { once: true, passive: true });
  document.addEventListener("click", () => { if(!stream) start(); }, { once: true });
  window.DaonSensors = { start, status: () => ({ ...sending, frames, pcmChunks, zeroChunks, fps, raw: RAW }) };
})();
