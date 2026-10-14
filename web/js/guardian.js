/* =========================================================
   guardian.js — 보호자 대시보드 메인 로직
   탭 전환, 긴급 팝업 플로우(3.2/4.4절), 평가 로그 기반 분석 탭(4.3절) 목업
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

/* ---------- 평가 로그 기반 분석 탭 초기화 (목업 데이터) ---------- */
let analysisInited = false;
function initAnalysisCharts(){
  if(analysisInited) return; analysisInited = true;

  renderTimeline("timelineChart", [
    { label:"pose", segments:[{start:0,end:8,pred:"no_fall"},{start:8,end:14,pred:"fall"},{start:14,end:30,pred:"no_fall"}] },
    { label:"audio", segments:[{start:0,end:9,pred:"no_fall"},{start:9,end:13,pred:"fall"},{start:13,end:30,pred:"no_fall"}] },
    { label:"radar", segments:[{start:0,end:10,pred:"no_fall"},{start:10,end:15,pred:"fall"},{start:15,end:30,pred:"no_fall"}] },
    { label:"융합", segments:[{start:0,end:9,pred:"no_fall"},{start:9,end:15,pred:"fall"},{start:15,end:30,pred:"no_fall"}] },
  ], 30);

  renderAccuracyBars("barChart", [
    { name:"카메라 단독", Recall:0.81, Precision:0.74, F1:0.77 },
    { name:"소리 단독",   Recall:0.68, Precision:0.80, F1:0.73 },
    { name:"레이더 단독", Recall:0.75, Precision:0.70, F1:0.72 },
    { name:"융합",       Recall:0.93, Precision:0.89, F1:0.91 },
  ]);

  renderConfMatrix("confMatrix", { tp:28, fn:3, fp:4, tn:65 });

  renderDelayHistogram("delayChart", [
    { label:"0-5s", count:4 }, { label:"5-10s", count:12 },
    { label:"10-15s", count:9 }, { label:"15-20s", count:3 }, { label:"20s+", count:1 },
  ]);

  const scenarioData = [
    { name:"S1_fall+소음", pose:"O", audio:"O", radar:"O", fused:"O", fa:"0.2/h" },
    { name:"S2_lying_down(비낙상)", pose:"X(오탐)", audio:"O", radar:"O", fused:"O", fa:"0.1/h" },
    { name:"S3_물건 떨어뜨림", pose:"O", audio:"X(오탐)", radar:"O", fused:"O", fa:"0.3/h" },
  ];
  const tbody = document.querySelector("#scenarioTable tbody");
  tbody.innerHTML = scenarioData.map(s=>`<tr><td>${s.name}</td><td>${s.pose}</td><td>${s.audio}</td><td>${s.radar}</td><td><b>${s.fused}</b></td><td>${s.fa}</td></tr>`).join("");

  renderEmotionWeek("emotionWeekChart", [
    {day:"월", happy:4, sad:1, angry:0, neutral:6},
    {day:"화", happy:3, sad:2, angry:1, neutral:5},
    {day:"수", happy:5, sad:0, angry:0, neutral:4},
    {day:"목", happy:2, sad:1, angry:0, neutral:7},
    {day:"금", happy:6, sad:0, angry:1, neutral:3},
  ]);

  document.getElementById("convoSummary").innerHTML = [
    "오전: 날씨와 식사 관련 대화, 기분 좋음 상태 유지",
    "오후: 복약 알림에 정상 응답, 산책 다녀오심",
    "저녁: 가족과의 통화 언급, 전반적으로 안정적",
  ].map(t=>`<li>${t}</li>`).join("");
}

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

/* ---------- 긴급 이벤트 시뮬레이션 (4.4절) ---------- */
let reAlertCount = 0;
let reAlertTimer = null;
let currentAlertId = null;

function simulateAlert(){
  currentAlertId = "a-" + Date.now();
  reAlertCount = 0;
  document.getElementById("reAlertCount").textContent = "0";
  document.getElementById("emgTime").textContent = "발생 시각: " + new Date().toLocaleTimeString("ko-KR",{hour12:false});
  document.getElementById("emgSensors").textContent = "판정 근거: pose(fall), audio(fall) 감지됨 · radar(no_motion)";
  document.getElementById("emergencyOverlay").classList.remove("hidden");

  document.getElementById("statusBadge").textContent = "주의";
  document.getElementById("statusBadge").className = "badge dot danger";
  document.getElementById("silhouette").textContent = "⚠️";

  addEventRow({ time:new Date().toLocaleTimeString("ko-KR",{hour12:false}), type:"낙상 의심", sensors:"pose+audio", status:"대기 중" });

  reAlertTimer = setInterval(()=>{
    reAlertCount++;
    document.getElementById("reAlertCount").textContent = reAlertCount;
    toast("🔔 미처리 알림 재발송 (" + reAlertCount + "회)");
  }, 8000); // 데모 편의상 8초 간격 (실제 기획은 30초)
}

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
    result:label, handler:"guardian_1"
  });
  toast(`처리 완료: ${label} (alert_id=${currentAlertId})`);
}

document.getElementById("btnCall119").addEventListener("click", ()=>{
  addEventRow({ time:new Date().toLocaleTimeString("ko-KR",{hour12:false}), type:"낙상 의심", result:"119 연결", handler:"guardian_1" });
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

/* ---------- 기기 연결 상태 데모: 가끔 레이더 끊김 시뮬레이션 ---------- */
setTimeout(()=>{
  document.getElementById("devRadar").textContent = "끊김";
  document.getElementById("devRadar").className = "badge warn dot";
  document.getElementById("connWarnBanner").classList.remove("hidden");
  toast("⚠ 레이더 센서 연결이 끊겼습니다 (마지막 수신 기준 경고)");
}, 9000);
