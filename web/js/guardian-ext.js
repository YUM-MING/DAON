/* =========================================================
   guardian-ext.js — 보호자 대시보드 확장 (통합자 추가분)
   - 헤더: 집 이름·집 코드·로그아웃·집 전환·집 참여
   - 개발자 도구 카드 (데모 집 / 개발자 계정): 가짜 사건 버튼 → /api/homes/{id}/dev/*
   - 이력 탭: 서버 알림 기록 / 리포트 탭: 요일별 감정·최근 대화 (DB)
   기존 guardian.js 는 손대지 않고 DOM 을 추가·채우기만 한다.
========================================================= */
(function(){
  if(!document.getElementById("emergencyOverlay") || !window.DaonReady) return;

  const el = (html) => { const d = document.createElement("div"); d.innerHTML = html.trim(); return d.firstElementChild; };
  const api = async (path, body) => {
    const r = await fetch(path, body ? { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body) } : {});
    const j = await r.json().catch(()=>({}));
    if(!r.ok) throw new Error(j.error || ("오류 " + r.status));
    return j;
  };

  DaonReady.then(({ me, home }) => {
    const hid = home.id;

    /* ---------- 헤더 ---------- */
    const header = document.querySelector(".gheader .wrap");
    if(header){
      const right = header.lastElementChild;
      const info = el(`<span class="badge" title="로봇 폰에서 이 코드를 입력하면 이 집에 연결됩니다">🏠 ${home.name} · 집 코드 <b>${home.code}</b></span>`);
      right.prepend(info);
      if(me.homes.length > 1){
        const sel = el(`<select class="btn sm outline" style="padding:6px 8px;">${me.homes.map(h => `<option value="${h.id}" ${h.id===hid?"selected":""}>${h.name}</option>`).join("")}</select>`);
        sel.addEventListener("change", () => { localStorage.setItem("daon_home_id", sel.value); location.reload(); });
        right.prepend(sel);
      }
      const join = el(`<button class="btn sm ghost" title="다른 집의 보호자로 참여">＋ 집 참여</button>`);
      join.addEventListener("click", async () => {
        const code = prompt("참여할 집의 6자리 코드");
        if(!code) return;
        try{ const h = await api("/api/homes/join", { code }); localStorage.setItem("daon_home_id", h.id); location.reload(); }
        catch(e){ toast(e.message); }
      });
      right.appendChild(join);
      const out = el(`<button class="btn sm ghost">로그아웃 (${me.user.name})</button>`);
      out.addEventListener("click", async () => { await api("/api/auth/logout", {}); localStorage.removeItem("daon_home_id"); location.href = "auth.html"; });
      right.appendChild(out);
    }

    /* ---------- 개발자 도구 ---------- */
    if(me.user.is_dev || home.is_demo){
      const card = el(`
        <div class="card mt16" id="devTools">
          <div class="card-head"><h3>🧪 개발자 도구 (시연용)</h3><span class="badge">가짜 센서 이벤트를 이 집에 주입</span></div>
          <div class="row" style="flex-wrap:wrap; gap:8px;">
            <button class="btn sm danger" data-act="scenario" data-arg="B">시나리오 B: 낙상 → 무응답 → 긴급</button>
            <button class="btn sm warn" data-act="scenario" data-arg="false_alarm">오탐: 낙상 → "괜찮아"</button>
            <button class="btn sm outline" data-act="scenario" data-arg="A">시나리오 A: 슬픈 표정 → 대화</button>
            <button class="btn sm outline" data-act="fall">낙상 (포즈+소리)</button>
            <button class="btn sm outline" data-act="unclear">신음 (인식 불가 소리)</button>
            <button class="btn sm outline" data-act="radar" data-arg="still">레이더: 정지</button>
            <button class="btn sm outline" data-act="radar" data-arg="moving">레이더: 움직임</button>
            <button class="btn sm outline" data-act="present" data-arg="0">사람 없음</button>
            <button class="btn sm outline" data-act="present" data-arg="1">사람 있음</button>
          </div>
          <div class="row mt8" style="gap:8px; align-items:center; flex-wrap:wrap;">
            <input id="devSpeech" placeholder="어르신이 말한 것처럼… (예: 응 괜찮아)" style="flex:1; min-width:220px; padding:8px 10px; border-radius:8px; border:1px solid rgba(127,127,127,.35); background:transparent; color:inherit;">
            <button class="btn sm" data-act="speech">발화 보내기</button>
            <select id="devEmotion" class="btn sm outline" style="padding:6px 8px;">
              <option value="neutral">중립</option><option value="happy">기쁨</option><option value="sad">슬픔</option><option value="angry">화남</option><option value="surprised">놀람</option>
            </select>
            <button class="btn sm outline" data-act="emotion">표정 보내기</button>
          </div>
          <p class="muted mt8" style="font-size:12px;">실제 폰이 연결돼 프레임을 보내는 중이면 카메라 판정이 0.5초마다 덮어쓰므로, 낙상 버튼은 폰 없이 시연할 때 쓰세요. 발화·표정·레이더는 언제나 유효합니다.</p>
        </div>`);
      document.getElementById("tab-home").appendChild(card);
      card.addEventListener("click", async (e) => {
        const b = e.target.closest("button[data-act]"); if(!b) return;
        const act = b.dataset.act, arg = b.dataset.arg;
        let body = {};
        if(act === "scenario") body = { name: arg };
        else if(act === "radar") body = { motion: arg };
        else if(act === "present") body = { present: arg === "1" };
        else if(act === "speech"){ body = { text: document.getElementById("devSpeech").value.trim() }; if(!body.text){ toast("문장을 입력하세요"); return; } }
        else if(act === "emotion") body = { label: document.getElementById("devEmotion").value };
        try{ await api(`/api/homes/${hid}/dev/${act}`, body); toast("주입: " + act + (arg ? " " + arg : "")); }
        catch(err){ toast(err.message); }
      });
    }

    /* ---------- 이력 탭: 서버 기록 ---------- */
    const t = window.DaonFmt ? DaonFmt.t : (ts) => new Date(ts*1000).toLocaleTimeString("ko-KR",{hour12:false});
    const RES = window.DaonFmt ? DaonFmt.RES_KO : {};
    api(`/api/homes/${hid}/alerts?limit=50`).then(list => {
      list.slice().reverse().forEach(a => {
        const s = a.sensors || {};
        if(typeof addEventRow !== "function") return;
        addEventRow({ time: t(a.created_ts), type: "낙상 의심", sensors: `pose:${s.pose} audio:${s.audio} radar:${s.radar}`,
                      status: a.resolved_ts ? "처리됨" : "대기 중", result: a.resolution ? (RES[a.resolution] || a.resolution) : "-", handler: a.handled_by || "-" });
      });
    }).catch(()=>{});

    /* ---------- 리포트 탭: DB 기반 ---------- */
    function renderReport(){
      api(`/api/homes/${hid}/report?days=7`).then(r => {
        if(r.emotion_week && r.emotion_week.length && typeof renderEmotionWeek === "function"){
          document.getElementById("emotionWeekChart").innerHTML = "";
          renderEmotionWeek("emotionWeekChart", r.emotion_week);
        }
        const ul = document.getElementById("convoSummary");
        if(ul){
          const items = (r.conversations || []).slice(-12).map(c => {
            const who = c.data.who === "robot" ? "🤖" : "👵";
            return `<li>${t(c.ts)} ${who} ${c.data.text}</li>`;
          });
          ul.innerHTML = items.length ? items.join("") : `<li class="muted">최근 7일 대화 기록이 없습니다</li>`;
        }
      }).catch(()=>{});
    }
    renderReport();
    document.addEventListener("daon:analysis", renderReport);      // 분석 탭 목업이 리포트를 덮어쓰면 다시 그림
    setInterval(renderReport, 30000);
  }).catch(()=>{});
})();
