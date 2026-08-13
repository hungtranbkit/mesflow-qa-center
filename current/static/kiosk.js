(() => {
  const screen = document.getElementById('screen');
  const app = document.getElementById('app');
  const kioskId = 'KIOSK-DEMO-01';
  let mode = 'WAIT_EMPLOYEE', buffer = '', worker = null, operation = null;
  let sessionStartedAt = null, goodQty = 0, ngQty = 0, messageTimer = null;
  const demoWorkers = {'WF|EMP|E001':'Nguyễn Văn A','WF|EMP|E002':'Trần Văn B'};
  const demoOps = {'WF|OP|OP001':{code:'OP001',name:'HÀN KHUNG XE',po:'PO-104',target:180,done:76},'WF|OP|OP002':{code:'OP002',name:'MÀI HOÀN THIỆN',po:'PO-105',target:240,done:121}};
  const elapsed = () => sessionStartedAt ? Math.floor((Date.now()-sessionStartedAt)/1000) : 0;
  const fmt = s => [Math.floor(s/3600),Math.floor((s%3600)/60),s%60].map(v=>String(v).padStart(2,'0')).join(':');
  function heartbeat(){fetch('/api/kiosk/heartbeat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kiosk_id:kioskId,state:mode,worker:worker?.name||null,operation:operation?.name||null,po:operation?.po||null,elapsed_seconds:elapsed(),input_buffer:buffer})}).catch(()=>{});}
  function render(){
    if(mode==='WAIT_EMPLOYEE') screen.innerHTML=`<div class="eyebrow">SẴN SÀNG</div><h1 class="headline">QUÉT THẺ NHÂN VIÊN</h1><div class="scan">██████████████</div><div class="hint">Không cần chạm màn hình</div>`;
    else if(mode==='WAIT_OPERATION') screen.innerHTML=`<div class="eyebrow">${worker.name}</div><h1 class="headline">QUÉT QR OPERATION</h1><div class="scan">██████████████</div><div class="hint">Tối đa 5 phút để nhận việc mới</div>`;
    else if(mode==='WORKING') screen.innerHTML=`<div class="eyebrow">${worker.name} · ${operation.po}</div><h1 class="headline">${operation.name}</h1><div class="badge">ĐANG LÀM</div><div class="timer">${fmt(elapsed())}</div><div class="metric-grid"><div class="metric">Mục tiêu ca<b>${operation.target}</b></div><div class="metric">Đã hoàn thành<b>${operation.done}</b></div><div class="metric">Tiến độ<b>${Math.round(operation.done/operation.target*100)}%</b></div></div><div class="hint">Quét lại thẻ để kết thúc và đổi operation</div>`;
    else if(mode==='INPUT_GOOD') screen.innerHTML=`<div class="eyebrow">KẾT THÚC SESSION · ${operation.name}</div><h1 class="headline">SỐ ĐẠT</h1><div class="number">${buffer||'0'}</div><div class="hint">Nhập bằng bàn phím số · ENTER để tiếp tục</div>`;
    else if(mode==='INPUT_NG') screen.innerHTML=`<div class="eyebrow">KẾT THÚC SESSION · ${operation.name}</div><h1 class="headline">SỐ LỖI</h1><div class="number">${buffer||'0'}</div><div class="hint">Nhập 0 nếu không có lỗi · ENTER để lưu</div>`;
    heartbeat();
  }
  function flash(kind,title,detail,next){clearTimeout(messageTimer);mode='MESSAGE';screen.innerHTML=`<h1 class="headline">${title}</h1><div class="sub">${detail||''}</div>`;screen.className='screen '+kind;heartbeat();messageTimer=setTimeout(()=>{screen.className='screen';next();},1200)}
  function scan(value){
    if(mode==='WAIT_EMPLOYEE'){
      const name=demoWorkers[value]; if(!name) return flash('error','THẺ KHÔNG HỢP LỆ','Quét lại',()=>{mode='WAIT_EMPLOYEE';render()});
      worker={code:value,name}; flash('success','ĐÃ NHẬN THẺ',name,()=>{mode='WAIT_OPERATION';render()});
    } else if(mode==='WAIT_OPERATION'){
      const op=demoOps[value]; if(!op) return flash('error','QR KHÔNG HỢP LỆ','Quét lại operation',()=>{mode='WAIT_OPERATION';render()});
      operation={...op};sessionStartedAt=Date.now();flash('success','BẮT ĐẦU LÀM',operation.name,()=>{mode='WORKING';render()});
    } else if(mode==='WORKING'){
      if(value!==worker.code) return flash('error','SAI NHÂN VIÊN','Quét đúng thẻ đang làm',()=>{mode='WORKING';render()});
      buffer='';mode='INPUT_GOOD';render();
    }
  }
  window.addEventListener('keydown',e=>{
    if(mode==='INPUT_GOOD'||mode==='INPUT_NG'){
      if(/^\d$/.test(e.key)){buffer=(buffer==='0'?'':buffer)+e.key;render();e.preventDefault();return}
      if(e.key==='Backspace'){buffer=buffer.slice(0,-1);render();e.preventDefault();return}
      if(e.key==='Enter'){
        const n=Number(buffer||0); if(mode==='INPUT_GOOD'){goodQty=n;buffer='';mode='INPUT_NG';render()} else {ngQty=n;operation.done+=goodQty;flash('success','ĐÃ CẬP NHẬT',`${goodQty} OK · ${ngQty} NG`,()=>{sessionStartedAt=null;operation=null;buffer='';mode='WAIT_OPERATION';render()})}; e.preventDefault();return
      }
    }
    if(mode==='MESSAGE') return;
    if(e.key==='Enter'){
      const v=buffer.trim();buffer='';if(v)scan(v);e.preventDefault();return
    }
    if(e.key.length===1) buffer+=e.key;
  });
  setInterval(()=>{document.getElementById('clock').textContent=new Date().toLocaleTimeString('vi-VN');if(mode==='WORKING')render();else heartbeat()},1000);
  app.focus();render();
})();
