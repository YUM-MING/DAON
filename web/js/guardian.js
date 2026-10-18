/* =========================================================
   guardian.js — 보호자 대시보드 메인 로직
   탭 전환, 긴급 팝업 처리, 이벤트 테이블. 데이터는 전부 서버(ws-client.js / guardian-ext.js)에서 온다
========================================================= */

/* ---------- 탭 전환 ---------- */
document.querySelectorAll(".tab-btn").forEach(btn=>{
  btn.addEventListener("click", ()=>{
    document.querySelectorAll(".tab-btn").forEach(b=>b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach(p=>p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("tab-"+btn.dataset.tab).classList.add("active");
  });
});

/* ---------- 개발자/평가 모드 토글 (4.3절: 일반 보호자 화면에서는 숨김) ---------- */
function toggleEvalMode(){
  const on = document.getElementById("evalModeToggle").checked;
  document.getElementById("evalHiddenNote").classList.toggle("hidden", on);
  document.getElementById("evalContent").classList.toggle("hidden", !on);
  if(on) initAnalysisCharts();
}

/* ---------- 분석 탭: 서버 평가 로그로 그린다 (ws-client.js 가 initAnalysisCharts 를 구현) ---------- */
let analysisInited = false;
function initAnalysisCharts(){ /* ws-client.js 에서 서버 데이터 기반으로 덮어씀 */ }

/* ---------- 홈 최근 이벤트 / 이력 테이블 공용 데이터 ---------- */
const eventLog = [];
function addEventRow(ev){
  eventLog.unshift(ev);
  const recentBody = document.getElementById("recentEventsBody");
  recentBody.innerHTML = eventLog.slice(0,5).map(e=>
    `<tr><td>${e.time}</td><td>${e.type}</td><td>${e.result||"-"}</td></tr>`).join("");

  const historyBody = document.getElementById("historyBody");
  historyBody.innerHTML = eventLog.map(e=>
    `<tr><td>${e.time}</td><td>${e.sensors||"-"}</td><td>${e.status||"-"}</td><td>${e.result||"-"}</td><td>${e.handler||"-"}</td></tr>`).join("");
}

/* ---------- 긴급 팝업 처리 (alert_created 는 ws-client.js 가 서버에서 받아 띄운다) ---------- */
let reAlertTimer = null;
let currentAlertId = null;

function resolveAlert(resolution){
  clearInterval(reAlertTimer);
  document.getElementById("emergencyOverlay").classList.add("hidden");
  document.getElementById("statusBadge").textContent = "안정";
  document.getElementById("statusBadge").className = "badge dot ok";
  document.getElementById("silhouette").textContent = "🧍";

  const label = { guardian_confirmed:"확인 완료", false_alarm:"오탐 처리", called_119:"119 연결" }[resolution] || resolution;
  addEventRow({
    time:new Date().toLocaleTimeString("ko-KR",{hour12:false}),
    type:"낙상 의심", sensors:"pose+audio", status:"처리됨",
    result:label, handler:(window.DaonMe && DaonMe.user.name) || "보호자"
  });
  toast(`처리 완료: ${label}`);
}

document.getElementById("btnCall119").addEventListener("click", ()=>{
  addEventRow({ time:new Date().toLocaleTimeString("ko-KR",{hour12:false}), type:"낙상 의심", result:"119 연결", handler:(window.DaonMe && DaonMe.user.name) || "보호자" });
});

/* ---------- 메시지 전달 모달 ---------- */
function openMsgModal(){ document.getElementById("msgModal").classList.remove("hidden"); }
function closeMsgModal(){ document.getElementById("msgModal").classList.add("hidden"); }
function sendMsgToRobot(){
  const msg = document.getElementById("msgInput").value.trim();
  if(!msg){ toast("메시지를 입력해주세요"); return; }
  toast("로봇 TTS로 전달됨: \"" + msg + "\"");
  document.getElementById("msgInput").value = "";
  closeMsgModal();
}
