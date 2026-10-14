/* =========================================================
   ws-client.js — 통합 서버 WebSocket 클라이언트 (통합자 추가분)
   기존 user-face.js / guardian.js 의 함수(setState, setEmotion, addEventRow …)를
   그대로 호출해서 화면을 바꾼다. UI 코드는 손대지 않는다.

   - 로봇 페이지   : localStorage 의 로봇 토큰으로 /ws/robot?token= 접속.
                    state 메시지 → setState/setEmotion/카운트다운, speak=true 면 브라우저 TTS 재생 후 tts_done 회신.
                    카메라/마이크 스트림은 robot-sensors.js 가 DaonWS.sendBinary 로 보낸다.
   - 보호자 페이지 : 세션 쿠키 + /ws/guardian?home=ID. 로그인 안 돼 있으면 auth.html 로.
                    alert_created / alert_resolved / device_status / robot_state / notice 처리.
   - 서버 없이 html 만 열면(file://) 아무것도 하지 않고 기존 데모 동작 유지.
========================================================= */
(function(){
  if(location.protocol === "file:") return;
  const isRobot = !!document.getElementById("face");
  const isGuardian = !!document.getElementById("emergencyOverlay");
  if(!isRobot && !isGuardian) return;
  const WS_BASE = (location.protocol === "https:" ? "wss://" : "ws://") + location.host;

  let ws = null, retry = 1000, wsUrl = null, stopped = false;
  function send(obj){ if(ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }
  function sendBinary(buf){ if(ws && ws.readyState === 1 && ws.bufferedAmount < 2_000_000) ws.send(buf); }
  function connected(){ return !!(ws && ws.readyState === 1); }
  function connect(){
    if(stopped || !wsUrl) return;
    ws = new WebSocket(wsUrl);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => { retry = 1000; send({type:"hello", role: isRobot ? "robot" : "guardian", info:{ua:navigator.userAgent}}); onConn(true); };
    ws.onclose = (e) => {
      onConn(false);
      if(e.code === 4401){ stopped = true; onUnauthorized(); return; }
      setTimeout(connect, retry); retry = Math.min(retry*2, 10000);
    };
    ws.onerror = () => { try{ ws.close(); }catch{} };
    ws.onmessage = (e) => { if(typeof e.data !== "string") return; let m; try{ m = JSON.parse(e.data); }catch{ return; } (isRobot ? onRobot : onGuardian)(m); };
  }
  function onConn(ok){
    if(isGuardian){
      const b = document.getElementById("connBadge");
      if(b){ b.textContent = ok ? "실시간 연결됨" : "서버 연결 끊김"; b.className = "badge dot " + (ok ? "ok" : "danger"); }
    }
    if(isRobot && typeof log === "function") log(ok ? "서버 연결됨" : "서버 연결 끊김 (재시도 중)");
    document.dispatchEvent(new CustomEvent("daon:conn", { detail:{ ok } }));
  }
  function onUnauthorized(){
    if(isRobot){ localStorage.removeItem("daon_robot_token"); location.href = "pair.html"; }
    else location.href = "auth.html?next=guardian-dashboard.html";
  }
  window.DaonWS = { send, sendBinary, connected };

  /* ============================ 로봇 얼굴 ============================ */
  const UI_EMOTION = { neutral:"neutral", happy:"happy", sad:"sad", angry:"angry", surprised:"neutral", fear:"sad" };
  // 로봇 얼굴 정책: 눈 모양은 상태(듣기/말하기/긴급) 가 기준. 감지된 어르신 감정은 happy 일 때만 같이 웃고,
  // sad/angry 는 얼굴에 그대로 비추지 않는다 (우울한 어르신에게 우울한 얼굴을 보여 주지 않기 위해). 배지에는 표시.
  const MIRROR = { happy:"happy", neutral:"neutral" };
  let curState = null, curText = null, curEmotion = null, cdTimer = null;

  function showCountdown(sec){
    if(typeof clearAlertTimer === "function") clearAlertTimer();   // user-face.js 의 자체 10초 타이머는 끈다 (서버가 기준)
    if(cdTimer){ clearInterval(cdTimer); cdTimer = null; }
    const ring = document.getElementById("countdownRing");
    if(!ring) return;
    if(sec === null || sec === undefined){ ring.classList.add("hidden"); return; }
    let remain = Math.round(sec);
    ring.classList.remove("hidden");
    ring.textContent = remain > 0 ? remain : "!";
    if(remain <= 0) return;
    cdTimer = setInterval(() => {
      remain -= 1;
      ring.textContent = remain > 0 ? remain : "!";
      if(remain <= 0){ clearInterval(cdTimer); cdTimer = null; }
    }, 1000);
  }

  function speak(text, utteranceId){
    const done = (ok) => { send({type:"tts_done", utterance_id: utteranceId, ok}); document.dispatchEvent(new CustomEvent("daon:tts", {detail:{speaking:false}})); };
    document.dispatchEvent(new CustomEvent("daon:tts", {detail:{speaking:true}}));
    if(!("speechSynthesis" in window)){ setTimeout(() => done(false), Math.max(1500, text.length*120)); return; }
    try{
      speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(text);
      u.lang = "ko-KR"; u.rate = 0.95;
      const ko = speechSynthesis.getVoices().find(v => (v.lang||"").toLowerCase().startsWith("ko"));
      if(ko) u.voice = ko;
      let finished = false;
      const finish = (ok) => { if(finished) return; finished = true; done(ok); };
      u.onend = () => finish(true);
      u.onerror = () => finish(false);
      setTimeout(() => finish(false), Math.max(4000, text.length*250));   // onend 가 안 오는 브라우저 대비
      speechSynthesis.speak(u);
    }catch(e){ done(false); }
  }

  function onRobot(m){
    if(m.type !== "state") return;
    const emo = UI_EMOTION[m.emotion] || "neutral";
    if(emo !== curEmotion){
      curEmotion = emo;
      if(typeof log === "function") log(`어르신 표정 감지: ${m.emotion} (raw=${m.emotion_raw||"-"} conf=${m.emotion_conf ?? "-"})`);
      const badge = document.getElementById("emotionBadge");
      if(MIRROR[emo] && typeof setEmotion === "function") setEmotion(MIRROR[emo]);
      else if(badge) badge.textContent = ({sad:"슬픔(감지)", angry:"화남(감지)"})[emo] || emo;
    }
    const text = m.text || "";
    const stateChanged = m.state !== curState;
    if(stateChanged || (text && text !== curText)){
      curState = m.state; curText = text;
      if(typeof setState === "function") setState(m.state, { text, countdown: m.state === "alert" ? m.countdown : undefined });
      if(typeof setContext === "function"){
        if(m.state === "speaking" || m.state === "recover") setContext("robotTalking");
        else if(m.state === "listening") setContext("userTalking");
        else if(m.state === "sleep") setContext("noUser");
      }
    } else if(m.state === "alert" && m.countdown != null && typeof startAlertCountdown === "function"){
      startAlertCountdown(m.countdown);
    }
    if(m.speak && text) speak(text, m.utterance_id);
  }

  /* ============================ 보호자 대시보드 ============================ */
  const EMO_KO = { neutral:"중립 😐", happy:"기쁨 😊", sad:"슬픔 😢", angry:"화남 😠", surprised:"놀람 😮" };
  const RES_KO = { elder_ok:"어르신 응답 (오탐 해제)", false_alarm:"오탐 처리", guardian_confirmed:"확인 완료",
                   called_119:"119 연결", timeout:"시간 초과" };
  const DEV_KO = { ok:"정상", mock:"모의", off:"미연결", error:"오류", stale:"끊김", starting:"시작 중", worker_ready:"대기" };
  let activeAlertId = null;
  const t = (ts) => new Date((ts||Date.now()/1000)*1000).toLocaleTimeString("ko-KR",{hour12:false});
  window.DaonFmt = { t, RES_KO, EMO_KO };

  function badge(el, status){
    if(!el) return;
    el.textContent = DEV_KO[status] || status;
    el.className = "badge dot " + (status === "ok" || status === "mock" ? "ok" : status === "off" ? "" : "warn");
  }

  function onGuardian(m){
    switch(m.type){
      case "alert_created": {
        activeAlertId = m.alert_id;
        const s = m.sensors || {};
        const emg = document.getElementById("emgSensors");
        if(emg) emg.textContent = `판정 근거: pose(${s.pose}) · audio(${s.audio}) · radar(${s.radar}) · 융합 ${m.fused ? m.fused.pred+" "+Math.round((m.fused.conf||0)*100)+"%" : ""}`;
        const tm = document.getElementById("emgTime");
        if(tm) tm.textContent = "발생 시각: " + t(m.ts) + (m.status && m.status !== "raised" ? ` (${m.status})` : "");
        document.getElementById("emergencyOverlay").classList.remove("hidden");
        const sb = document.getElementById("statusBadge");
        if(sb){ sb.textContent = "주의"; sb.className = "badge dot danger"; }
        const sil = document.getElementById("silhouette"); if(sil) sil.textContent = "⚠️";
        if(typeof addEventRow === "function")
          addEventRow({ time:t(m.ts), type: m.status === "realert" ? "재알림" : "낙상 의심", sensors:`pose:${s.pose} audio:${s.audio} radar:${s.radar}`, status:"대기 중" });
        if(typeof toast === "function") toast("🚨 " + (m.title || "긴급 알림"));
        break;
      }
      case "alert_resolved": {
        document.getElementById("emergencyOverlay").classList.add("hidden");
        const sb = document.getElementById("statusBadge");
        if(sb){ sb.textContent = "안정"; sb.className = "badge dot ok"; }
        const sil = document.getElementById("silhouette"); if(sil) sil.textContent = "🧍";
        if(typeof addEventRow === "function")
          addEventRow({ time:t(m.ts), type:"낙상 의심", status:"처리됨", result: RES_KO[m.resolution] || m.resolution, handler: m.handled_by || "-" });
        activeAlertId = null;
        break;
      }
      case "device_status": {
        const rows = document.querySelectorAll("#tab-home table tr");
        // 행 순서: 로봇 폰 / 레이더 / 카메라 / 마이크 / Wi-Fi  (guardian-dashboard.html)
        const map = [["robot", m.robot_online ? "ok" : "off"], ["radar", m.radar], ["camera", m.camera], ["mic", m.mic], ["wifi", m.wifi]];
        map.forEach(([, st], i) => { const r = rows[i]; if(r){ badge(r.querySelector(".badge"), st === "unknown" ? "off" : st); } });
        if(rows[0] && m.battery != null){ const c = rows[0].querySelectorAll("td")[2]; if(c) c.textContent = "배터리 " + m.battery + "%"; }
        const warn = [m.radar, m.camera, m.mic].some(s => s === "error" || s === "stale");
        const banner = document.getElementById("connWarnBanner"); if(banner) banner.classList.toggle("hidden", !warn);
        break;
      }
      case "robot_state": {
        const he = document.getElementById("homeEmotion"); if(he) he.textContent = EMO_KO[m.emotion] || m.emotion;
        const hu = document.getElementById("homeUpdated"); if(hu) hu.textContent = t() + " · " + (m.state||"");
        break;
      }
      case "debug": {
        document.dispatchEvent(new CustomEvent("daon:debug", { detail: m }));
        break;
      }
      case "notice": {
        if(typeof toast === "function") toast("💬 " + (m.title || "안부 알림") + ": " + (m.body||"").split("\n")[0]);
        if(typeof addEventRow === "function") addEventRow({ time:t(m.ts), type:"정서 알림", result:(m.body||"").split("\n")[0] });
        break;
      }
    }
  }

  /* ============================ 시작 ============================ */
  if(isRobot){
    const token = localStorage.getItem("daon_robot_token");
    if(!token){ location.href = "pair.html"; return; }
    wsUrl = WS_BASE + "/ws/robot?token=" + encodeURIComponent(token);
    window.DaonReady = fetch("/api/robot/me?token=" + encodeURIComponent(token)).then(r => {
      if(r.status === 401){ onUnauthorized(); throw new Error("unauthorized"); }
      return r.json();
    }).then(info => {
      window.DaonRobot = info; connect();
      const hi = document.getElementById("robotHomeInfo");
      if(hi && info.home) hi.textContent = `연결된 집: ${info.home.name} (코드 ${info.home.code})`;
      return info;
    });
  }

  if(isGuardian){
    // guardian.js 의 버튼 핸들러를 감싸서 서버로도 보낸다 (함수 선언은 전역이라 재할당 가능)
    const origResolve = window.resolveAlert, origSend = window.sendMsgToRobot;
    window.resolveAlert = function(resolution){
      send({type:"alert_resolve", alert_id: activeAlertId, resolution});
      if(origResolve) origResolve(resolution);
    };
    window.sendMsgToRobot = function(){
      const input = document.getElementById("msgInput");
      const msg = input ? input.value.trim() : "";
      if(msg) send({type:"message_to_robot", text: msg});
      if(origSend) origSend();
    };
    const btn119 = document.getElementById("btnCall119");
    if(btn119) btn119.addEventListener("click", () => send({type:"alert_resolve", alert_id: activeAlertId, resolution:"called_119"}));

    window.DaonReady = fetch("/api/me").then(r => {
      if(r.status === 401){ onUnauthorized(); throw new Error("unauthorized"); }
      return r.json();
    }).then(me => {
      const saved = parseInt(localStorage.getItem("daon_home_id") || "0", 10);
      const home = me.homes.find(h => h.id === saved) || me.homes[0];
      if(!home){ throw new Error("no home"); }
      localStorage.setItem("daon_home_id", home.id);
      window.DaonMe = me; window.DaonHome = home;
      wsUrl = WS_BASE + "/ws/guardian?home=" + home.id;
      connect();
      return { me, home };
    });

    // 분석 탭: 서버 평가 로그(jsonl)로 타임라인·정확도 그래프를 그린다 (실패하면 기존 목업)
    const origInit = window.initAnalysisCharts;
    window.initAnalysisCharts = async function(){
      try{
        const hid = window.DaonHome ? window.DaonHome.id : 0;
        const [log, summary] = await Promise.all([
          fetch(`/api/homes/${hid}/eval_log?limit=600`).then(r => r.json()),
          fetch("/api/eval_summary").then(r => r.json()),
        ]);
        if(!Array.isArray(log) || log.length < 2) throw new Error("no log");
        const t0 = log[0].ts, total = Math.max(1, log[log.length-1].ts - t0);
        const seg = (key) => {
          const out = []; let cur = null;
          log.forEach(l => {
            const p = (l[key] && l[key].pred) || "no_fall"; const ts = l.ts - t0;
            if(!cur || cur.pred !== p){ if(cur) cur.end = ts; cur = {start: ts, end: ts, pred: p === "uncertain" ? "no_fall" : p}; out.push(cur); }
            cur.end = ts;
          });
          if(cur) cur.end = total;
          return out;
        };
        renderTimeline("timelineChart", [
          { label:"pose", segments: seg("pose") }, { label:"audio", segments: seg("audio") },
          { label:"radar", segments: seg("radar") }, { label:"융합", segments: seg("fused") },
        ], total);
        const cam = summary && summary.camera;
        if(cam && cam.f1 != null){
          renderAccuracyBars("barChart", [{ name:"카메라 단독 (UR Fall)", Recall:cam.recall, Precision:cam.precision, F1:cam.f1 }]);
          const c = cam.confusion || {};
          renderConfMatrix("confMatrix", { tp:c.TP||0, fn:c.FN||0, fp:c.FP||0, tn:c.TN||0 });
        }
        analysisInited = true;
      }catch(e){
        if(origInit) origInit();
      }
      document.dispatchEvent(new CustomEvent("daon:analysis"));
    };
  }
})();
