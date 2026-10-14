/* =========================================================
   common.js — 모든 페이지 공통: 테마 스위처, 토스트, 코너 제스처,
   그리고 "가짜 메시지(Mock WebSocket)" 버스
   (실제 서버 연동 전까지 UI를 단독으로 테스트하기 위한 장치 — 기획서 6장/7장 참고)
========================================================= */

/* ---------- 테마 ----------
   UI 디자인은 "미니멀"로 확정되었습니다. 과거에는 애니메이션/픽셀아트 테마를
   실시간으로 전환할 수 있는 스위처가 있었으나, 디자인 확정에 따라 전환 기능은
   정리(제거)하고 미니멀 테마만 고정으로 적용합니다. */
const FIXED_THEME = "theme-minimal";

function applyTheme(themeId){
  document.body.classList.add(themeId);
  localStorage.setItem("sc_theme", themeId);
}

function mountThemeSwitcher(){
  // 더 이상 여러 테마를 제공하지 않으므로 전환 버튼 바는 생성하지 않고,
  // 확정된 미니멀 테마 클래스만 body에 적용합니다.
  applyTheme(FIXED_THEME);
}

/* ---------- 토스트 ---------- */
function toast(msg){
  let area = document.querySelector(".toast-area");
  if(!area){ area = document.createElement("div"); area.className="toast-area"; document.body.appendChild(area); }
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  area.appendChild(el);
  setTimeout(()=> el.remove(), 2600);
}

/* ---------- 모드 재설정: 화면 모서리 5초 길게 누르기 (기획서 1장) ---------- */
function mountCornerReset(onReset){
  const el = document.createElement("div");
  el.className = "corner-reset";
  el.innerHTML = '<div class="progress"></div>';
  document.body.appendChild(el);
  let timer=null, start=0;
  const DURATION = 5000;
  function tick(){
    const p = Math.min(1, (Date.now()-start)/DURATION);
    el.style.setProperty("--p", (p*100)+"%");
    if(p>=1){ cancel(); toast("모드 재설정: 랜딩 화면으로 이동합니다"); localStorage.removeItem("sc_mode"); location.href="index.html"; return; }
    timer = requestAnimationFrame(tick);
  }
  function begin(){ el.classList.add("pressing"); start=Date.now(); tick(); }
  function cancel(){ el.classList.remove("pressing"); el.style.setProperty("--p","0%"); if(timer) cancelAnimationFrame(timer); timer=null; }
  el.addEventListener("mousedown", begin);
  el.addEventListener("touchstart", begin, {passive:true});
  ["mouseup","mouseleave","touchend","touchcancel"].forEach(ev=> el.addEventListener(ev, cancel));
}

/* ---------- Mock 이벤트 버스 (실서버의 WebSocket을 대체) ---------- */
const MockBus = (() => {
  const listeners = {};
  function on(type, cb){ (listeners[type] ??= []).push(cb); }
  function emit(type, payload){ (listeners[type]||[]).forEach(cb=>cb(payload)); }
  return { on, emit };
})();

document.addEventListener("DOMContentLoaded", mountThemeSwitcher);
