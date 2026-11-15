/* =========================================================
   report-range.js — 리포트 탭 "기준일 · 기간" 선택 (대화 내용 통계 · 상태 요약 카드가 함께 씀)
   - 기간 = 기준일(끝나는 날짜) 포함 N일 (1/3/7). 기본: 오늘 · 7일 → 기존 화면과 같은 요청(end 없이 days=7)
   - "오늘"·최대 날짜는 서버 응답의 today(서버 로컬 날짜)만 쓴다 (브라우저 시계를 쓰지 않음)
   - 선택이 바뀌면 document 에 "daon:report-range" 이벤트 → 두 카드가 각자 다시 불러온다
   - AI 요약 패널·주간 감정 통계(guardian-ext.js)도 daon:report-range 를 받아 선택한 기간으로 다시 그린다
   - 서버의 today 를 처음 알게 되거나 바뀌면 "daon:report-today" 이벤트 (감정 차트가 날짜 기준 요청으로 바꿈)
========================================================= */
(function(){
  const input = document.getElementById("reportEnd");
  const todayBtn = document.getElementById("reportEndToday");
  const label = document.getElementById("reportRangeLabel");
  const dayBtns = Array.from(document.querySelectorAll("[data-report-days]"));
  const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

  const state = { end: null, days: 7, today: null };   // end=null: 오늘 (서버 기준)

  const md = (s) => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s || "")); return m ? `${+m[2]}/${+m[3]}` : String(s || ""); };
  function rangeText(from, to){                          // "11/8 ~ 11/14", 하루면 "11/14"
    if(!from || !to) return "";
    return from === to ? md(to) : `${md(from)} ~ ${md(to)}`;
  }
  const isToday = () => state.end === null || (state.today !== null && state.end === state.today);
  function get(){ return { end: state.end, days: state.days, today: state.today, isToday: isToday() }; }
  function query(){                                      // 오늘이면 end 를 보내지 않는다 (기존 요청과 동일)
    let q = `days=${encodeURIComponent(state.days)}`;
    if(!isToday()) q += `&end=${encodeURIComponent(state.end)}`;
    return q;
  }

  function paint(){
    dayBtns.forEach(b => {
      const on = Number(b.dataset.reportDays) === state.days;
      b.classList.toggle("outline", !on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }
  function set(patch){
    Object.assign(state, patch);
    if(label) label.textContent = "";                    // 새 응답이 오면 다시 채움
    paint();
    document.dispatchEvent(new CustomEvent("daon:report-range", { detail: get() }));
  }

  /* 카드가 서버 응답을 받을 때마다 부른다: 서버의 today 로 최대 날짜·기본값, from~to 로 범위 표시 */
  function applyResponse(r){
    if(!r || typeof r !== "object") return;
    let todayChanged = false;
    if(typeof r.today === "string" && DATE_RE.test(r.today)){
      todayChanged = state.today !== r.today;
      state.today = r.today;
      if(input){
        input.max = r.today;
        if(state.end === null) input.value = r.today;
      }
    }
    const current = Number(r.days) === state.days && (isToday() ? r.is_today !== false : r.end === state.end);
    if(label && current) label.textContent = rangeText(r.from, r.to);
    paint();
    if(todayChanged) document.dispatchEvent(new CustomEvent("daon:report-today", { detail: get() }));
  }

  if(input){
    input.addEventListener("change", () => {
      const v = input.value;
      if(!DATE_RE.test(v)) return;                         // 지우는 중 등 미완성 값은 무시
      if(state.today && v > state.today){                  // 직접 입력한 미래 날짜: 되돌린다 (서버도 400)
        input.value = state.end || state.today;
        return;
      }
      set({ end: state.today && v === state.today ? null : v });
    });
  }
  if(todayBtn) todayBtn.addEventListener("click", () => {
    if(input && state.today) input.value = state.today;
    set({ end: null });
  });
  dayBtns.forEach(b => b.addEventListener("click", () => {
    const n = Number(b.dataset.reportDays);
    if(n && n !== state.days) set({ days: n });
  }));
  paint();

  window.DaonReportRange = { get, query, applyResponse, rangeText };
})();
