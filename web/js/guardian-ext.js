/* =========================================================
   guardian-ext.js — 보호자 대시보드 확장 (통합자 추가분)
   - 헤더: 집 이름·집 코드·로그아웃·집 전환·집 참여 (서버 모드에서는 '낙상 시뮬레이션' 버튼 숨김)
   - 개발자 도구 카드 (데모 집 / 개발자 계정): 가짜 사건 버튼 → /api/homes/{id}/dev/*
   - 실시간 센서 로그: 서버가 보내는 debug 메시지(포즈·낙상 상태·표정 raw/conf·소리 분류·STT)를 그대로 표시
     → "왜 낙상으로 봤나 / 왜 슬프다고 봤나 / 말을 왜 못 알아들었나" 를 여기서 바로 본다
   - 이력 탭: 서버 알림 기록 / 리포트 탭: 요일별 감정·최근 대화 (DB)
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
  const t = window.DaonFmt ? DaonFmt.t : (ts) => new Date(ts*1000).toLocaleTimeString("ko-KR",{hour12:false});

  // 서버 모드에서는 목업 시뮬레이션 버튼 대신 개발자 도구를 쓴다
  document.querySelectorAll(".gheader button").forEach(b => { if(b.textContent.includes("시뮬레이션")) b.style.display = "none"; });

  DaonReady.then(({ me, home }) => {
    const hid = home.id;

    /* ---------- 헤더 ---------- */
    const header = document.querySelector(".gheader .wrap");
    if(header){
      const right = header.lastElementChild;
      const info = el(`<span class="badge" title="로봇 폰에서 이 코드를 입력하면 이 집에 연결됩니다">🏠 ${home.name} · 집 코드 <b>${home.code}</b></span>`);
      header.firstElementChild.appendChild(info);
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

    /* ---------- 실시간 센서 로그 (모든 보호자에게 표시, 접을 수 있음) ---------- */
    const logCard = el(`
      <div class="card mt16" id="sensorLogCard">
        <div class="card-head"><h3>📡 실시간 센서 로그</h3>
          <label class="eval-toggle"><input type="checkbox" id="sensorLogToggle" checked> 표시</label></div>
        <div class="row" style="gap:14px; flex-wrap:wrap; font-size:13px;" id="sensorNow">
          <span>포즈: <b id="snPose">-</b></span><span>자세: <b id="snPosture">-</b></span><span>낙상 상태: <b id="snFall">-</b></span>
          <span>표정: <b id="snEmo">-</b></span><span>기분: <b id="snMood">-</b></span><span>소리: <b id="snAudio">-</b></span><span>레이더: <b id="snRadar">-</b></span>
        </div>
        <div class="dev-log mt8" id="sensorLog"></div>
        <p class="muted mt8" style="font-size:12px;">낙상 오탐이면 "속도·종횡비·각도" 값을, 표정 오류면 "raw·conf·얼굴px" 를, 음성 오류면 "소리 분류·STT" 줄을 보고 조정합니다. 파일: <code>logs/${home.code}/</code></p>
      </div>`);
    document.getElementById("tab-home").appendChild(logCard);
    const logBox = document.getElementById("sensorLog");
    const MAX = 150;
    function addLog(cls, text){
      if(!document.getElementById("sensorLogToggle").checked) return;
      const d = document.createElement("div"); d.className = cls; d.textContent = text;
      logBox.prepend(d);
      while(logBox.children.length > MAX) logBox.lastChild.remove();
    }
    const set = (id, v) => { const e = document.getElementById(id); if(e) e.textContent = v; };
    document.addEventListener("daon:debug", (e) => {
      const m = e.detail, ts = t(m.ts);
      switch(m.kind){
        case "vision":
          set("snPose", `${m.pred} ${m.conf}`); set("snPosture", m.note ? `${m.posture} (${m.note})` : m.posture); set("snFall", m.fall_state);
          addLog(m.pred === "fall" ? "fall" : "", `${ts} 포즈 ${m.pred}(${m.conf}) 자세=${m.posture}${m.note ? " ⚠ " + m.note : ""} 상태=${m.fall_state} 속도=${m.median_vel} 종횡비=${m.aspect} 각도=${m.torso_angle} 사람conf=${m.person_conf} 하반신=${m.lower_body} 급락=${m.drop_recent} 누움=${m.lying_sec}s`);
          break;
        case "emotion":
          set("snEmo", `${m.expression || m.raw} (${m.conf}, 얼굴 ${m.face_px}px)`);
          addLog("emo", `${ts} 표정 ${m.expression || m.raw} conf=${m.conf} 얼굴=${m.face_px}px → 기분 ${m.mood || "-"}`);
          break;
        case "mood": {   // 1초마다: 상태 칸만 갱신 (로그는 바뀔 때만)
          const d = m.detail || {};
          set("snMood", `${m.pred} (${m.conf}${d.intensity ? ", " + d.intensity : ""}${d.baseline_ready ? ", 기준선✔" : ""})`);
          break;
        }
        case "mood_changed": {
          const d = m.detail || {};
          addLog("emo", `${ts} 😊 기분 → ${m.pred} conf=${m.conf} 안정=${d.stability} 주표정=${d.top_expression} 기준선차=${d.baseline_delta ?? "-"} 얼굴=${d.face_px}px`);
          break;
        }
        case "audio":
          set("snAudio", `${m.label} ${m.conf} (낙상확률 ${m.p_fall})${m.muted ? " 🔇" : ""}`);
          if(m.label !== "ambient" || m.p_fall >= 0.3) addLog(m.label === "fall" ? "fall" : "", `${ts} 소리 ${m.label}(${m.conf}) P(fall)=${m.p_fall}${m.muted ? " [TTS중 음소거]" : ""}`);
          break;
        case "audio_fall": addLog("fall", `${ts} 🔊 낙상음 이벤트 conf=${m.confidence}`); break;
        case "speech": addLog("speech", `${ts} 🗣 STT "${m.text}" (${m.duration}s)`); break;
        case "voice_unclear": addLog("speech", `${ts} 🗣 목소리는 났으나 인식 실패 (${m.duration}s)`); break;
        case "fall_detected": addLog("fall", `${ts} ⚠ 비전 낙상 확정`); break;
        case "radar": set("snRadar", `${m.motion} ${m.distance_cm||""}cm`); break;
        case "dialogue": addLog("", `${ts} 🤖 ${m.strategy} "${m.text||""}"  (${m.reason||""})`); break;
      }
    });

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
          <p class="muted mt8" style="font-size:12px;">실제 폰이 프레임을 보내는 중이면 카메라 판정이 0.5초마다 덮어쓰므로, 낙상 버튼은 폰 없이 시연할 때 쓰세요. 발화·표정·레이더는 언제나 유효합니다.</p>
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
        const ew = document.getElementById("emotionWeekChart");
        if(r.emotion_week && r.emotion_week.length && typeof renderEmotionWeek === "function"){
          ew.innerHTML = "";
          renderEmotionWeek("emotionWeekChart", r.emotion_week);
        } else if(ew){ ew.innerHTML = '<p class="muted">최근 7일 표정 기록이 없습니다</p>'; }
        const ul = document.getElementById("convoSummary");
        if(ul){
          const items = (r.conversations || []).slice(-12).map(c => `<li>${t(c.ts)} ${c.data.who === "robot" ? "🤖" : "👵"} ${c.data.text}</li>`);
          ul.innerHTML = items.length ? items.join("") : `<li class="muted">최근 7일 대화 기록이 없습니다</li>`;
        }
      }).catch(()=>{});
    }
    renderReport();
    document.addEventListener("daon:analysis", renderReport);      // 분석 탭 목업이 리포트를 덮어쓰면 다시 그림
    setInterval(renderReport, 30000);
  }).catch(()=>{});
})();
