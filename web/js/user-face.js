/* =========================================================
   user-face.js — 사용자용(어르신용) 로봇 얼굴 상태 머신
   기획서 2.1(Zero-Touch 흐름), 2.4(감정 매핑), 3장(오탐 UX)을 목업으로 구현
========================================================= */
const face = document.getElementById("face");
const stateBadge = document.getElementById("stateBadge");
const emotionBadge = document.getElementById("emotionBadge");
const eyeBadge = document.getElementById("eyeBadge");
const contextBadge = document.getElementById("contextBadge");
const speech = document.getElementById("speech");
const countdownRing = document.getElementById("countdownRing");
const logList = document.getElementById("logList");
const panel = document.getElementById("devPanel");

const STATE_LABEL = {
  sleep:"대기(sleep)", wake:"기상(wake)", listening:"듣는 중(listening)",
  thinking:"생각 중(thinking)", speaking:"말하는 중(speaking)",
  alert:"긴급(alert)", recover:"복귀(recover)",
};
const EMOTION_LABEL = { neutral:"중립", happy:"기쁨", sad:"슬픔", angry:"화남(차분)" };
const STATE_SPEECH = {
  sleep:"( 졸고 있어요… )", wake:"안녕하세요! 😊", listening:"네, 듣고 있어요",
  thinking:"음... 생각 중이에요", speaking:"오늘 기분은 어떠세요?",
  alert:"괜찮으세요?! 응답해주세요", recover:"다행이에요. 다시 평소처럼 지낼게요",
};

/* 레퍼런스 10종 눈 디자인 + 긴급 모드 눈(11번째) 라벨 */
const EYE_LABEL = {
  default:"기본형", happy:"행복한 눈", curious:"호기심", sleepy:"졸린 눈",
  sparkle:"반짝이는 눈", side:"옆눈", heart:"하트 눈", joy:"기쁨의 눈",
  square:"네모형 눈", ring:"링형 눈", emergency:"긴급 눈",
};

/* 상태(state)별 기본 눈 모양 자동 매핑
   alert(긴급) 상태는 전용 "긴급 눈"으로 매핑하여 위급 상황을 눈 모양으로도 즉시 전달한다 */
const STATE_DEFAULT_EYE = {
  sleep:"sleepy", wake:"curious", listening:"curious",
  thinking:"default", speaking:"default", alert:"emergency", recover:"happy",
};
/* 감정(emotion)별 기본 눈 모양 자동 매핑 */
const EMOTION_DEFAULT_EYE = {
  neutral:"default", happy:"happy", sad:"side", angry:"square",
};

/* =========================================================
   상황별(context) 눈 자동 매핑 — 요청 시나리오 6가지
   정책: "마지막으로 누른 버튼이 항상 즉시 화면에 반영"된다.
   (상태/감정/상황/수동 눈 선택 중 어느 것을 누르든 이전 선택을 바로 덮어쓰고,
    진행 중이던 반복 타이머가 있다면 즉시 정리한다 — 그래야 아무 때나 바로 확인 가능)
   ========================================================= */
const CONTEXT_LABEL = {
  idle:"평상시", happy:"행복한 상태", userTalking:"사용자가 말할 때",
  noUser:"사용자 없음/대화 없음", robotTalking:"로봇이 말할 때", userMoving:"사용자 움직임 감지",
};
let sparkleTimer = null;     // 로봇이 말하는 중 호기심↔반짝임 눈을 주기적으로 교차
let movingResetTimer = null; // 움직임 감지 시 잠깐 옆눈 보여준 뒤 이전 컨텍스트로 복귀
let currentContext = "idle";
let prevContextBeforeMoving = "idle";

function clearContextTimers(){
  if(sparkleTimer){ clearInterval(sparkleTimer); sparkleTimer = null; }
  if(movingResetTimer){ clearTimeout(movingResetTimer); movingResetTimer = null; }
}

function setContext(context){
  clearContextTimers(); // 어떤 상황을 선택하든 이전에 돌던 반복 타이머는 즉시 중단
  currentContext = context;
  contextBadge.textContent = CONTEXT_LABEL[context] || context;
  log(`상황 전환 → ${CONTEXT_LABEL[context]}`);

  switch(context){
    case "idle":
      // 평상시(기본): 특별한 입력이 없는 보통 상태 → 기본형 눈
      applyEye("default");
      break;

    case "happy":
      // 공감-기쁨 감정이 활성화된 상태 → 행복한 눈
      applyEye("happy");
      break;

    case "userTalking":
      // 사용자(어르신)가 말하는 중, 로봇이 듣고 있는 상태 → 호기심 눈(집중해서 듣는 느낌)
      applyEye("curious");
      break;

    case "noUser":
      // 사람이 화면/센서 범위에 안 보이거나 일정 시간 대화가 없을 때 → 졸린 눈
      applyEye("sleepy");
      break;

    case "robotTalking":
      // 로봇이 말하는 중: 평소엔 호기심 눈을 유지하다가, 말하는 중간중간
      // 짧게 반짝이는 눈으로 전환되었다가 다시 돌아오는 것을 반복 (생동감 연출)
      applyEye("curious");
      sparkleTimer = setInterval(()=>{
        applyEye("sparkle");
        setTimeout(()=>{
          if(currentContext === "robotTalking") applyEye("curious");
        }, 500);
      }, 2200);
      break;

    case "userMoving":
      // 사용자가 움직이는 것을 레이더/카메라가 감지한 순간 → 옆눈으로 반응
      // 일정 시간 후에는 직전 상황(대화 중이었는지, 평상시였는지)으로 자동 복귀
      prevContextBeforeMoving = (currentContext === "userMoving") ? prevContextBeforeMoving : currentContext;
      applyEye("side");
      movingResetTimer = setTimeout(()=>{
        setContext(prevContextBeforeMoving || "idle");
      }, 1800);
      break;
  }
}

let alertTimer = null;
let alertRemaining = 10;
/* 통합 서버에 붙어 있는지 (ws-client.js / robot-sensors.js 가 동작하는 조건). file:// 로 열면 기존 데모 동작 */
const SERVER_MODE = location.protocol !== "file:" && !!localStorage.getItem("daon_robot_token");

function log(msg){
  const el = document.createElement("div");
  const t = new Date().toLocaleTimeString("ko-KR",{hour12:false});
  el.textContent = `[${t}] ${msg}`;
  logList.prepend(el);
}

function togglePanel(){ panel.classList.toggle("open"); }

/* 화면에 눈 모양만 즉시 반영하는 저수준 헬퍼 (로그/뱃지 텍스트는 건드리지 않음) */
function applyEye(eye){
  face.dataset.eye = eye;
  eyeBadge.textContent = EYE_LABEL[eye] || eye;
}

function setState(state, opts={}){
  clearAlertTimer();
  clearContextTimers();       // 상태를 바꾸면 이전에 돌던 반짝임/복귀 타이머는 중단
  currentContext = "idle";
  contextBadge.textContent = "평상시";
  face.dataset.state = state;
  stateBadge.textContent = STATE_LABEL[state];
  stateBadge.className = "badge dot " + (state==="alert" ? "danger" : state==="recover" ? "ok" : "");
  speech.textContent = opts.text || STATE_SPEECH[state] || "";
  countdownRing.classList.toggle("hidden", state !== "alert");
  log(`상태 전환 → ${STATE_LABEL[state]}`);

  // 상태를 바꾸면 그 상태에 매핑된 눈이 항상 즉시 화면에 반영됨
  applyEye(STATE_DEFAULT_EYE[state] || "default");

  if(state === "alert"){
    // 서버가 countdown 초를 주면 그 값으로, null 이면 카운트다운 없이(비상 유지 중), 없으면 기본 10초
    if(opts.countdown === null) countdownRing.classList.add("hidden");
    else startAlertCountdown(opts.countdown ?? 10);
  }
}

function setEmotion(emotion){
  clearContextTimers();       // 감정을 바꾸면 이전에 돌던 반짝임/복귀 타이머는 중단
  currentContext = "idle";
  contextBadge.textContent = "평상시";
  face.dataset.emotion = emotion;
  emotionBadge.textContent = EMOTION_LABEL[emotion];
  log(`감정 전환 → ${EMOTION_LABEL[emotion]}`);

  // 감정을 바꾸면 그 감정에 매핑된 눈이 항상 즉시 화면에 반영됨
  applyEye(EMOTION_DEFAULT_EYE[emotion] || "default");
}

/* 레퍼런스 10종 눈 디자인 수동 전환 — 패널에서 직접 클릭하면 항상 즉시 반영된다 */
function setEye(eye){
  clearContextTimers();
  currentContext = "idle";
  contextBadge.textContent = "수동 지정";
  applyEye(eye);
  log(`눈 디자인 수동 전환 → ${EYE_LABEL[eye]}`);
}

/* 현재 state/emotion 기준 자동 매핑으로 눈만 복귀 (상태 자체는 바꾸지 않음) */
function resetEyeAuto(){
  clearContextTimers();
  currentContext = "idle";
  contextBadge.textContent = "평상시";
  const state = face.dataset.state;
  const emotion = face.dataset.emotion;
  applyEye(EMOTION_DEFAULT_EYE[emotion] || STATE_DEFAULT_EYE[state] || "default");
  log("눈 디자인 자동 매핑으로 복귀");
}

function startAlertCountdown(sec = 10){
  clearAlertTimer();
  alertRemaining = Math.round(sec);
  countdownRing.textContent = alertRemaining > 0 ? alertRemaining : "!";
  if(alertRemaining <= 0) return;           // 0 = 이미 보호자 알림 단계
  alertTimer = setInterval(()=>{
    alertRemaining -= 1;
    countdownRing.textContent = alertRemaining > 0 ? alertRemaining : "!";
    if(alertRemaining <= 0){
      clearAlertTimer();
      if(SERVER_MODE){ log("응답 대기 시간 종료 (판정은 서버가 함)"); return; }
      log("10초 경과 → 긴급 확정, 보호자에게 팝업 전송");
      toast("보호자에게 긴급 알림이 전송되었습니다 (guardian-dashboard.html 확인)");
    }
  }, 1000);
}
function clearAlertTimer(){ if(alertTimer){ clearInterval(alertTimer); alertTimer=null; } }

/* ---------- 3.1절 오탐 UX 시나리오 ---------- */
function runScenario(type){
  log(`--- 시나리오 실행: ${type} ---`);
  setState("alert", { text:"낙상 의심! 괜찮으세요?" });

  const after = (ms, fn) => setTimeout(fn, ms);

  if(type === "positive"){
    after(1800, ()=>{
      clearAlertTimer();
      setEmotion("happy");
      setState("recover", { text:'"괜찮아" 응답 인식 → 오탐으로 해제' });
      log("보호자 측: 긴급 팝업 없음, 로그에 '오탐 해제' 기록");
      after(2200, ()=> setState("sleep"));
    });
  } else if(type === "help"){
    after(1500, ()=>{
      speech.textContent = '"도와줘" 인식 → 즉시 에스컬레이션 (카운트다운 건너뜀)';
      log("즉시 에스컬레이션: 보호자에게 즉시 긴급 팝업 전송");
      toast("즉시 긴급 팝업이 보호자에게 전송되었습니다");
    });
  } else if(type === "noresponse"){
    log("응답 없음 + 움직임 없음 → 10초 카운트다운 진행");
    // 기본 startAlertCountdown 그대로 진행, 완료 시 긴급 확정 메시지 출력됨
  } else if(type === "motion"){
    after(4000, ()=>{
      clearAlertTimer();
      speech.textContent = "다시 한 번 말씀해 주세요";
      log("레이더가 움직임 감지 → 재질문 1회");
      after(3500, ()=>{
        speech.textContent = "응답이 없어 보호자에게 알립니다";
        log("재질문 후에도 응답 없음 → 긴급 확정");
        toast("보호자에게 긴급 알림이 전송되었습니다");
      });
    });
  } else if(type === "unclear"){
    after(4000, ()=>{
      clearAlertTimer();
      speech.textContent = "다시 한 번 말씀해 주세요";
      log("애매한 응답/인식 실패 → 재질문 1회");
      after(3500, ()=>{
        speech.textContent = "확인이 어려워 보호자에게 알립니다";
        log("재질문 후에도 불확실 → 긴급 쪽으로 판단");
        toast("보호자에게 긴급 알림이 전송되었습니다");
      });
    });
  }
}

/* ---------- 모서리 제스처(모드 재설정) ---------- */
mountCornerReset();

/* ---------- 초기 진입 데모: 상태 전환과 함께 상황별 눈 매핑도 함께 시연 (서버 연결 시에는 생략) ---------- */
setState("sleep");
setContext("noUser");                          // 대기 중 → 졸린 눈
if(!SERVER_MODE){
  setTimeout(()=> setState("wake"), 2200);
  setTimeout(()=> setContext("idle"), 2200);      // 기상 → 평상시 기본형 눈
  setTimeout(()=> setState("listening"), 4200);
  setTimeout(()=> setContext("userTalking"), 4200); // 어르신이 말하는 중 → 호기심 눈
  setTimeout(()=> setState("thinking"), 6200);
  setTimeout(()=> setState("speaking"), 7600);
  setTimeout(()=> setContext("robotTalking"), 7600); // 로봇이 말하는 중 → 호기심↔반짝임 교차
}

/* ---------- 로봇 연결 해제 (사용자 모드의 '로그아웃') ---------- */
function unpairRobot(){
  if(!confirm("이 기기를 집에서 연결 해제할까요? 다시 쓰려면 집 코드를 입력해야 합니다.")) return;
  localStorage.removeItem("daon_robot_token");
  localStorage.removeItem("daon_home_name");
  localStorage.removeItem("sc_mode");
  location.href = "pair.html";
}
