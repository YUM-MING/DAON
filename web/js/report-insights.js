/* =========================================================
   report-insights.js — 리포트 탭 "이번 주 어르신 상태 요약" 카드
   - GET /api/homes/{id}/report/insights?days=7 (서버: server/core/report_insights.py)
   - 규칙 기반 참고 신호를 보여 준다 (진단·예측 아님, 임계값은 검증되지 않은 개발용 초기값)
   - 호출 시점: 리포트 탭으로 전환할 때, "새로고침" 버튼을 누를 때뿐 (자동 갱신 없음)
   - 응답 값은 전부 textContent 로만 그린다 (innerHTML 에 넣지 않음)
========================================================= */
(function(){
  const card = document.getElementById("reportInsightsCard");
  const body = document.getElementById("reportInsightsBody");
  const refreshBtn = document.getElementById("reportInsightsRefresh");
  const disclaimerEl = document.getElementById("reportInsightsDisclaimer");
  if(!card || !body) return;

  const DAYS = 7;
  const FIXED_NOTE = "대화 내용과 기록을 바탕으로 한 참고용 주의 신호이며 진단이나 예측이 아닙니다.";
  const CLEAR_NOTE = "건강·정서 상태가 안정적이라는 뜻이 아니라, 수집된 데이터에서 설정된 주의 신호가 확인되지 않았다는 뜻입니다.";
  const ALERT_FIXED = "알림 이력은 종합 단계 계산에 반영되지 않습니다.";
  const LEVEL_CLASS = { "확인 권장":"ri-lv-check", "관찰 필요":"ri-lv-watch", "판단 보류":"ri-lv-pending", "특이 신호 없음":"ri-lv-clear" };
  const SIGNAL_ORDER = { "확인":0, "주의":1, "참고":2 };
  const SIGNAL_CLASS = { "확인":"ri-sg-check", "주의":"ri-sg-warn", "참고":"ri-sg-note" };
  const ALERT_TYPE_KO = { no_response:"응답 없음", danger:"몸 불편 표현", crisis:"긴급 표현", emotional:"마음이 힘든 표현" };

  /* ---------- DOM 도우미 (텍스트는 항상 textContent) ---------- */
  function h(tag, cls, text){
    const e = document.createElement(tag);
    if(cls) e.className = cls;
    if(text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  const str = (v) => (v === undefined || v === null ? "" : String(v));
  const num = (v) => (typeof v === "number" && isFinite(v) ? v : 0);
  const arr = (v) => (Array.isArray(v) ? v : []);
  const obj = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : {});

  /* 개발자/평가 모드: guardian.js 와 같은 방식으로 헤더 체크박스(#evalModeToggle)의 checked 를 본다 */
  const devToggle = document.getElementById("evalModeToggle");
  const devMode = () => !!(devToggle && devToggle.checked);
  if(devToggle) devToggle.addEventListener("change", () => {      // 다시 불러오지 않고 하단 줄만 보이기/숨기기
    body.querySelectorAll(".ri-foot").forEach(e => { e.hidden = !devMode(); });
  });

  function section(title){
    const s = h("div", "ri-sec");
    s.appendChild(h("h4", "ri-sec-title", title));
    return s;
  }
  function showMessage(text, cls){
    body.replaceChildren(h("p", "muted ri-msg" + (cls ? " " + cls : ""), text));
  }
  function when(ts){                                  // epoch 초 → "11/9 14:05"
    if(typeof ts !== "number" || !isFinite(ts)) return "시각 미상";
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getMonth() + 1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  /* ---------- 렌더링 ---------- */
  function render(r){
    r = obj(r);
    if(disclaimerEl) disclaimerEl.textContent = str(r.disclaimer) || FIXED_NOTE;
    const out = [];

    // 종합 단계 배지 + 요약
    const level = str(r.level) || "단계 정보 없음";
    const head = h("div", "ri-head");
    const badge = h("span", "ri-level " + (LEVEL_CLASS[level] || "ri-lv-other"), level);
    head.appendChild(badge);
    if(level === "특이 신호 없음") head.appendChild(h("span", "ri-clear-note", CLEAR_NOTE));
    out.push(head);
    if(str(r.summary)) out.push(h("p", "ri-summary", r.summary));

    // 데이터 기준
    const sf = obj(r.sufficiency);
    const period = r.from && r.to ? ` (${str(r.from)} ~ ${str(r.to)})` : "";
    out.push(h("div", "muted ri-basis",
      `분석된 어르신 발화 ${num(sf.analyzed)}건 · 분석된 날 ${num(sf.days_with_data)}일${period}`));
    // 판단 보류면 summary 가 이미 이유를 말하므로 이유 상자는 생략 (그 외 단계에서 데이터가 부족할 때만 표시)
    if(sf.enough === false && str(sf.reason) && level !== "판단 보류") out.push(h("div", "ri-reason", `ⓘ ${str(sf.reason)}`));

    // 신호
    const sigSec = section("확인된 신호");
    const signals = arr(r.signals).map((s, i) => ({ s: obj(s), i }))
      .sort((a, b) => ((SIGNAL_ORDER[str(a.s.level)] ?? 3) - (SIGNAL_ORDER[str(b.s.level)] ?? 3)) || (a.i - b.i));
    if(!signals.length){
      sigSec.appendChild(h("p", "muted ri-none", "확인된 신호가 없습니다"));
    } else {
      const ul = h("ul", "ri-signals");
      signals.forEach(({ s }) => {
        const li = h("li", "ri-signal");
        const top = h("div", "ri-signal-top");
        top.appendChild(h("span", "ri-chip " + (SIGNAL_CLASS[str(s.level)] || "ri-sg-other"), str(s.level) || "-"));
        top.appendChild(h("span", "ri-signal-title", str(s.title)));
        if(s.affects_level === false){
          const tag = h("span", "ri-ref", "참고용");
          tag.title = "표시만 하고 종합 단계 계산에는 넣지 않은 신호입니다";
          top.appendChild(tag);
        }
        li.appendChild(top);
        if(str(s.detail)) li.appendChild(h("div", "ri-signal-detail", s.detail));
        ul.appendChild(li);
      });
      sigSec.appendChild(ul);
    }
    out.push(sigSec);

    // 알림 이력 (단계와 별개, 항상 표시)
    const al = obj(r.alerts);
    const items = arr(al.items);
    const count = typeof al.count === "number" ? al.count : items.length;
    const alSec = section(count > 0 ? `알림 이력 ${count}건` : "알림 이력");
    if(count > 0){
      const ul = h("ul", "ri-alerts");
      items.forEach(it => {
        it = obj(it);
        const type = str(it.type);
        ul.appendChild(h("li", "", `${when(it.ts)} · ${type ? (ALERT_TYPE_KO[type] || type) : "유형 정보 없음"}`));
      });
      alSec.appendChild(ul);
    } else {
      alSec.appendChild(h("p", "muted ri-none", "기간 내 알림 이력 없음"));
    }
    const note = str(al.note);
    if(note) alSec.appendChild(h("div", "muted ri-small", note));
    if(!(/종합\s*단계/.test(note) && /반영/.test(note))){        // note 가 이미 설명하면 고정 문구는 생략
      alSec.appendChild(h("div", "muted ri-small", ALERT_FIXED));
    }
    out.push(alSec);

    // 보호자 확인 권장 사항
    const recs = arr(r.recommendations).map(str).filter(Boolean);
    if(recs.length){
      const recSec = section("보호자 확인 권장 사항");
      const ul = h("ul", "ri-recs");
      recs.forEach(x => ul.appendChild(h("li", "", x)));
      recSec.appendChild(ul);
      out.push(recSec);
    }

    // 직전 기간 비교 (수치만)
    const comp = obj(r.comparison);
    if(comp.available === false){
      out.push(h("div", "muted ri-small ri-compare-off", str(comp.reason) || "직전 기간과 비교할 수 없습니다"));
    } else if(comp.available === true){
      const det = h("details", "ri-compare");
      det.appendChild(h("summary", "", "직전 기간과 수치 비교"));
      const ul = h("ul", "ri-changes");
      arr(r.changes).forEach(c => {
        c = obj(c);
        const prev = c.previous === null || c.previous === undefined ? "" : ` (직전 ${str(c.previous)})`;
        ul.appendChild(h("li", "", `${str(c.label)} ${str(c.current)}${prev}`));
      });
      det.appendChild(ul);
      out.push(det);
    }

    // 근거·버전 (개발자/평가 모드에서만 보임)
    if(r.basis || r.version){
      const foot = h("div", "muted ri-foot", `근거: ${str(r.basis) || "-"} · ${str(r.version) || "-"}`);
      foot.hidden = !devMode();
      out.push(foot);
    }
    body.replaceChildren(...out);
  }

  /* ---------- 불러오기 ---------- */
  async function getJSON(path){
    const r = await fetch(path, { credentials: "same-origin" });
    let j = null;
    try{ j = await r.json(); }
    catch(e){ j = null; }                            // 본문이 JSON 이 아님 → 아래에서 오류로 처리
    if(!r.ok) throw new Error((j && j.error) || `서버 오류 (${r.status})`);
    if(!j || typeof j !== "object") throw new Error("응답 형식이 올바르지 않습니다");
    return j;
  }

  let loading = false;
  async function load(){
    if(loading) return;                              // 탭 연타·버튼 연타 시 중복 호출 방지
    loading = true;
    if(refreshBtn) refreshBtn.disabled = true;
    showMessage("상태 요약을 불러오는 중…");
    try{
      if(!window.DaonReady) throw new Error("서버에 연결된 화면에서만 볼 수 있습니다");
      const info = await window.DaonReady;
      const hid = info && info.home && info.home.id;
      if(!hid) throw new Error("집 정보를 찾지 못했습니다");
      render(await getJSON(`/api/homes/${encodeURIComponent(hid)}/report/insights?days=${DAYS}`));
    }catch(err){
      console.warn("[report-insights]", err);
      showMessage(`상태 요약을 불러오지 못했습니다: ${err && err.message ? err.message : err}`, "ri-error");
    }finally{
      loading = false;
      if(refreshBtn) refreshBtn.disabled = false;
    }
  }

  /* ---------- 리포트 탭으로 전환할 때만 호출 (report-stats.js 와 같은 방식: 탭 버튼에 리스너만 추가) ---------- */
  const activeTab = () => { const b = document.querySelector(".tab-btn.active"); return b ? b.dataset.tab : null; };
  let lastTab = activeTab();
  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const prev = lastTab;
      lastTab = btn.dataset.tab;
      if(btn.dataset.tab === "report" && prev !== "report") load();
    });
  });
  if(refreshBtn) refreshBtn.addEventListener("click", load);
  const panel = document.getElementById("tab-report");
  if(panel && panel.classList.contains("active")) load();
  else showMessage("리포트 탭을 열면 상태 요약을 불러옵니다");

  window.DaonReportInsights = { render, load };       // 화면 점검용
})();
