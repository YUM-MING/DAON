/* =========================================================
   charts.js — 외부 차트 라이브러리 없이 순수 DOM/CSS로 그리는
   경량 차트 렌더러 (분석/평가 탭 전용, 기획서 4.3절 A~E)
========================================================= */

// A. 센서별 판정 타임라인 (pose/audio/radar/fused 4줄)
function renderTimeline(containerId, rows, totalDuration){
  const el = document.getElementById(containerId);
  el.innerHTML = "";
  rows.forEach(row=>{
    const rowEl = document.createElement("div");
    rowEl.className = "tl-row";
    rowEl.innerHTML = `<div class="tl-label">${row.label}</div><div class="tl-track"></div>`;
    const track = rowEl.querySelector(".tl-track");
    row.segments.forEach(seg=>{
      const s = document.createElement("div");
      s.className = "tl-seg " + (seg.pred === "fall" ? "fall" : "nofall");
      s.style.left = (seg.start/totalDuration*100)+"%";
      s.style.width = ((seg.end-seg.start)/totalDuration*100)+"%";
      s.title = `${seg.pred} (${seg.start}s~${seg.end}s)`;
      track.appendChild(s);
    });
    el.appendChild(rowEl);
  });
}

// B. 정확도 비교 막대그래프 (Recall/Precision/F1 x 4방식)
function renderAccuracyBars(containerId, data){
  const el = document.getElementById(containerId);
  el.innerHTML = "";
  const colors = { Recall:"var(--accent)", Precision:"var(--accent-2)", F1:"var(--success)" };
  data.forEach(method=>{
    ["Recall","Precision","F1"].forEach(metric=>{
      const val = method[metric];
      const group = document.createElement("div");
      group.className = "bar-group";
      group.innerHTML = `
        <div class="bg-label"><span>${method.name} · ${metric}</span><span>${(val*100).toFixed(0)}%</span></div>
        <div class="bar-track"><div class="bar-fill" style="width:${val*100}%; background:${colors[metric]}"></div></div>`;
      el.appendChild(group);
    });
  });
}

// D. 혼동행렬 2x2
function renderConfMatrix(containerId, m){
  // m = {tp, fn, fp, tn}
  const el = document.getElementById(containerId);
  el.innerHTML = `
    <div class="conf-grid">
      <div></div><div class="hd">예측: 낙상</div><div class="hd">예측: 비낙상</div>
      <div class="hd">실제: 낙상</div><div class="cell tp">${m.tp}</div><div class="cell fn">${m.fn}</div>
      <div class="hd">실제: 비낙상</div><div class="cell fp">${m.fp}</div><div class="cell tn">${m.tn}</div>
    </div>`;
}

// E. 알림 지연 분포 (막대 히스토그램)
function renderDelayHistogram(containerId, buckets){
  // buckets = [{label:'0-5s', count:n}, ...]
  const el = document.getElementById(containerId);
  const max = Math.max(...buckets.map(b=>b.count), 1);
  el.innerHTML = `<div class="delay-bars">${
    buckets.map(b=>`<div class="delay-bar" style="height:${(b.count/max*100)}%"><span>${b.count}</span></div>`).join("")
  }</div>
  <div class="row mt8" style="font-size:11px; color:var(--muted); justify-content:space-between;">
    ${buckets.map(b=>`<div>${b.label}</div>`).join("")}
  </div>`;
}

// 리포트 탭: 주간 감정 통계 (단순 막대)
function renderEmotionWeek(containerId, days){
  // days = [{day:'월', happy:3,sad:1,angry:0,neutral:5}]
  const el = document.getElementById(containerId);
  el.innerHTML = days.map(d=>{
    const total = d.happy+d.sad+d.angry+d.neutral;
    return `<div class="bar-group">
      <div class="bg-label"><span>${d.day}요일</span><span class="muted">총 ${total}회</span></div>
      <div class="bar-track">
        <div class="bar-fill" style="width:${d.happy/total*100}%; background:#ffd35b" title="기쁨 ${d.happy}"></div>
        <div class="bar-fill" style="width:${d.neutral/total*100}%; background:#8fa6ff" title="중립 ${d.neutral}"></div>
        <div class="bar-fill" style="width:${d.sad/total*100}%; background:#6b8cff" title="슬픔 ${d.sad}"></div>
        <div class="bar-fill" style="width:${d.angry/total*100}%; background:#ff8a6b" title="화남 ${d.angry}"></div>
      </div>
    </div>`;
  }).join("");
}
