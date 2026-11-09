/* =========================================================
   report-stats.js — 리포트 탭 "최근 7일 대화 내용 통계" 카드
   - GET /api/homes/{id}/report/stats?days=7 (서버: server/core/report_stats.py)
   - 어르신이 직접 말한 내용의 '언급 횟수'만 보여준다 (측정·진단 아님)
   - 호출 시점: 리포트 탭을 열 때, "새로고침" 버튼을 누를 때뿐 (자동 갱신 없음)
   - 응답 값은 전부 textContent 로만 그린다 (innerHTML 에 넣지 않음)
   - 대화 속 '불안'(emotion_text.anxious)은 카메라 표정 통계의 '두려움'(fear)과 다른 데이터다
========================================================= */
(function(){
  const card = document.getElementById("reportStatsCard");
  const body = document.getElementById("reportStatsBody");
  const refreshBtn = document.getElementById("reportStatsRefresh");
  if(!card || !body) return;

  const DAYS = 7;
  const SYMPTOM_KO = { pain:"아프다", ache:"쑤신다", numb:"저린다", stiff:"뻐근하다", cold_ache:"시리다", throb:"욱신거린다", dizzy:"어지럽다" };
  const EMOTION_KO = { lonely:"외로움", sad:"슬픔", anxious:"불안", angry:"화남", lethargic:"무기력", grief:"그리움" };
  const EMOTION_HIDDEN = new Set(["positive"]);     // 취향 문장("좋아해")이 섞여 기분 지표로 쓰지 않는다
  const WANTS = [["food", "먹고 싶다고 하신 것"], ["like_food", "좋아한다고 하신 음식"],
                 ["place", "가고 싶다고 하신 곳"], ["person", "보고 싶다고 하신 사람"]];
  const WEEKDAY = ["일", "월", "화", "수", "목", "금", "토"];

  /* ---------- DOM 도우미 (텍스트는 항상 textContent) ---------- */
  function h(tag, cls, text){
    const e = document.createElement(tag);
    if(cls) e.className = cls;
    if(text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  const num = (v) => (typeof v === "number" && isFinite(v) ? v : 0);
  const times = (n) => (n > 0 ? `${n}회` : "언급 없음");

  function section(title, note){
    const s = h("div", "rs-sec");
    const t = h("h4", "rs-sec-title", title);
    if(note) t.appendChild(h("span", "rs-sec-note", note));
    s.appendChild(t);
    return s;
  }
  function kvRows(pairs){                            // [["수면 부족", 2], ...] → 한 줄씩
    const ul = h("ul", "rs-list");
    pairs.forEach(([label, n]) => {
      const li = h("li", n > 0 ? "" : "rs-zero");
      li.appendChild(h("span", "", label));
      li.appendChild(h("b", "", times(n)));
      ul.appendChild(li);
    });
    return ul;
  }

  function showMessage(text, cls){
    body.replaceChildren(h("p", "muted rs-msg" + (cls ? " " + cls : ""), text));
  }

  /* ---------- 렌더링 ---------- */
  function dayLabel(day){                            // "2026-11-09" → "11/9 (월)"
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(day || ""));
    if(!m) return String(day || "");
    const d = new Date(+m[1], +m[2] - 1, +m[3]);
    return `${+m[2]}/${+m[3]} (${WEEKDAY[d.getDay()]})`;
  }

  function render(r){
    const convo = Array.isArray(r.conversations) ? r.conversations : [];
    const userTotal = convo.reduce((a, c) => a + num(c.user_msgs), 0);
    const robotTotal = convo.reduce((a, c) => a + num(c.robot_msgs), 0);
    const out = [];

    // 상단 요약
    const top = h("div", "rs-summary");
    top.appendChild(h("div", "rs-summary-main", `어르신 발화 ${userTotal}건 · 로봇 응답 ${robotTotal}건`));
    const period = r.from && r.to ? ` (${r.from} ~ ${r.to})` : "";
    top.appendChild(h("div", "muted rs-sub", `그중 내용 분석에 포함된 어르신 발화 ${num(r.analyzed)}건${period}`));
    const sk = r.skipped || {};
    const skipped = [["중복 입력", num(sk.duplicate)], ["알아듣기 어려운 발화", num(sk.stt_unclear)],
                     ["지난 대화를 묻는 질문", num(sk.recall_question)], ["분석 오류", num(sk.analysis_error)]]
      .filter(([, n]) => n > 0).map(([k, n]) => `${k} ${n}건`);
    if(skipped.length) top.appendChild(h("div", "muted rs-sub", `분석에서 제외: ${skipped.join(", ")}`));
    out.push(top);

    if(userTotal === 0){
      out.push(h("p", "muted rs-msg", `최근 ${DAYS}일간 분석할 대화가 없습니다`));
      body.replaceChildren(...out);
      return;
    }

    // 날짜별 발화 건수 (작은 막대)
    const daysSec = section("날짜별 어르신 발화");
    const maxUser = Math.max(1, ...convo.map(c => num(c.user_msgs)));
    convo.forEach(c => {
      const row = h("div", "rs-day");
      row.title = `어르신 발화 ${num(c.user_msgs)}건 · 로봇 응답 ${num(c.robot_msgs)}건`;
      row.appendChild(h("span", "rs-day-label", dayLabel(c.day)));
      const track = h("div", "bar-track rs-day-track");
      const fill = h("div", "bar-fill rs-day-fill");
      fill.style.width = (num(c.user_msgs) / maxUser * 100) + "%";
      track.appendChild(fill);
      row.appendChild(track);
      row.appendChild(h("span", "rs-day-count", `${num(c.user_msgs)}건`));
      daysSec.appendChild(row);
    });
    out.push(daysSec);

    if(num(r.analyzed) === 0){
      out.push(h("p", "muted rs-msg", "내용 분석에 포함된 발화가 없습니다"));
      body.replaceChildren(...out);
      return;
    }

    const grid = h("div", "rs-grid");

    // 수면
    const sl = r.sleep || {};
    const sleepSec = section("수면");
    sleepSec.appendChild(kvRows([["수면 부족 언급", num(sl.bad)], ["피곤하다는 언급", num(sl.tired)], ["잘 잤다는 언급", num(sl.good)]]));
    grid.appendChild(sleepSec);

    // 식사
    const ml = r.meal || {};
    const mealSec = section("식사");
    mealSec.appendChild(kvRows([["식사했다는 언급", num(ml.done)], ["식사 안 했다는 언급", num(ml.not)], ["식사 여부를 물으신 횟수", num(ml.ask)]]));
    grid.appendChild(mealSec);

    // 증상
    const symSec = section("몸 불편 언급");
    const syms = Array.isArray(r.symptoms) ? r.symptoms : [];
    if(!syms.length){
      symSec.appendChild(h("p", "muted rs-none", "언급 없음"));
    } else {
      const ul = h("ul", "rs-list");
      syms.forEach(s => {
        const li = h("li");
        const part = s.part ? String(s.part) : "부위 미상";
        const kind = SYMPTOM_KO[s.kind] || String(s.kind || "");
        li.appendChild(h("span", "", [part, kind].filter(Boolean).join(" · ")));
        const right = h("span", "rs-right");
        if(s.danger){
          const b = h("span", "badge warn rs-badge", "주의");
          b.title = "급히 확인이 필요할 수 있는 표현과 함께 말씀하셨습니다";
          right.appendChild(b);
        }
        right.appendChild(h("b", "", `${num(s.count)}회 · ${num(s.days)}일`));
        li.appendChild(right);
        ul.appendChild(li);
      });
      symSec.appendChild(ul);
    }
    grid.appendChild(symSec);

    // 감정 표현 (말로 한 것)
    const emoSec = section("감정 표현", "말씀하신 내용 기준 · 카메라 표정 통계와 별개");
    const emo = Object.entries(r.emotion_text || {})
      .filter(([k, n]) => !EMOTION_HIDDEN.has(k) && num(n) > 0)
      .sort((a, b) => num(b[1]) - num(a[1]));
    if(!emo.length){
      emoSec.appendChild(h("p", "muted rs-none", "언급 없음"));
    } else {
      emoSec.appendChild(kvRows(emo.map(([k, n]) => [EMOTION_KO[k] || k, num(n)])));
    }
    grid.appendChild(emoSec);
    out.push(grid);

    // 먹고 싶은 것 · 좋아하는 음식 · 가고 싶은 곳 · 보고 싶은 사람 (빈 줄은 숨김)
    const w = r.wants || {};
    const wantRows = WANTS.map(([key, label]) => {
      const items = (Array.isArray(w[key]) ? w[key] : []).filter(x => x && x.item).slice(0, 5);
      if(!items.length) return null;
      const row = h("div", "rs-want");
      row.appendChild(h("div", "rs-want-label", label));
      const chips = h("div", "rs-chips");
      items.forEach(x => chips.appendChild(h("span", "rs-chip", `${x.item} ${num(x.count)}회`)));
      row.appendChild(chips);
      return row;
    }).filter(Boolean);
    if(wantRows.length){
      const ws = section("말씀하신 바람·취향");
      wantRows.forEach(x => ws.appendChild(x));
      out.push(ws);
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
    showMessage("대화 내용 통계를 불러오는 중…");
    try{
      if(!window.DaonReady) throw new Error("서버에 연결된 화면에서만 볼 수 있습니다");
      const info = await window.DaonReady;
      const hid = info && info.home && info.home.id;
      if(!hid) throw new Error("집 정보를 찾지 못했습니다");
      const r = await getJSON(`/api/homes/${encodeURIComponent(hid)}/report/stats?days=${DAYS}`);
      render(r);
    }catch(err){
      console.warn("[report-stats]", err);
      showMessage(`통계를 불러오지 못했습니다: ${err && err.message ? err.message : err}`, "rs-error");
    }finally{
      loading = false;
      if(refreshBtn) refreshBtn.disabled = false;
    }
  }

  /* ---------- 리포트 탭을 열 때만 호출 (guardian.js 탭 버튼과 같은 버튼에 리스너만 추가) ---------- */
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
  if(panel && panel.classList.contains("active")) load();   // 리포트 탭이 이미 열린 상태로 시작한 경우
  else showMessage("리포트 탭을 열면 통계를 불러옵니다");

  window.DaonReportStats = { render, load };          // 화면 점검용
})();
