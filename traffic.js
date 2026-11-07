(() => {
  'use strict';
  const anchor=document.getElementById('traffic-short')||document.querySelector('.weather-strip');
  if(!anchor || document.getElementById('traffic-live'))return;
  const panel=document.createElement('section');
  panel.id='traffic-live';
  panel.className='card';
  panel.style.margin='8px 0';
  panel.setAttribute('aria-label','高松周辺の交通情報');
  const title=document.createElement('strong');title.textContent='高松周辺の道路情報';
  const button=document.createElement('button');button.type='button';button.className='btn sm secondary';button.textContent='更新';
  const status=document.createElement('p');status.setAttribute('role','status');
  const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent='場所・詳細';details.append(summary);
  const list=document.createElement('ul');details.append(list);
  const css=document.createElement('style');
  css.textContent=`
#traffic-live.card{padding:10px 12px;margin:6px 0!important}
#traffic-live .traffic-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
#traffic-live .traffic-head strong{font-size:14px;line-height:1.35;margin-right:auto}
#traffic-live .traffic-head button{min-height:36px;padding:5px 10px;font-size:12px}
#traffic-live .traffic-stamp{font-size:11px;color:var(--sub);white-space:nowrap}
#traffic-live p{margin:6px 0 2px;font-size:14px;line-height:1.5}
#traffic-live summary{font-size:12px;min-height:32px;display:list-item;line-height:32px;cursor:pointer}
#traffic-live ul{max-height:45vh;overflow-y:auto;margin:4px 0 0;padding-left:22px;font-size:13px;line-height:1.6}
#traffic-live li+li{margin-top:6px}
`;
  const head=document.createElement('div');head.className='traffic-head';
  const stamp=document.createElement('small');stamp.className='traffic-stamp';
  head.append(title,stamp,button);
  panel.append(css,head,status,details);anchor.insertAdjacentElement('afterend',panel);
  const labels={accident:'事故',roadClosed:'通行止め',jam:'渋滞',roadWorks:'工事',other:'その他'};
  let busy=false,lastAttempt=0,expiry;
  function unavailable(){
    status.textContent='交通情報を取得できません。下の確認先をご利用ください。';
    list.replaceChildren();details.hidden=true;stamp.textContent='';
  }
  async function update(){
    if(busy || Date.now()-lastAttempt<60000)return;
    busy=true;lastAttempt=Date.now();button.disabled=true;
    clearTimeout(expiry);details.hidden=true;list.replaceChildren();status.textContent='交通情報を確認中…';
    try{
      const response=await fetch('https://takamatsu-traffic-proxy.shikishimanipponn5.workers.dev/api/v1/traffic',{
        cache:'no-store',credentials:'omit',signal:AbortSignal.timeout(20000)
      });
      if(!response.ok)throw new Error();
      const data=await response.json();
      const age=Date.now()-Date.parse(data.updatedAt);
      if(data.status!=='ok'||data.schemaVersion!==1||!Number.isFinite(age)||age<0||age>900000||
        !Array.isArray(data.incidents)||!Number.isInteger(data.total)||data.total<0||
        !data.counts||Object.keys(labels).some(k=>!Number.isInteger(data.counts[k])||data.counts[k]<0)||
        Object.values(data.counts).reduce((a,b)=>a+b,0)!==data.total)throw new Error();
      const time=new Date(data.updatedAt).toLocaleTimeString('ja-JP',{timeZone:'Asia/Tokyo',hour:'2-digit',minute:'2-digit'});
      status.textContent=Object.entries(labels).filter(([key])=>data.counts[key]>0)
        .map(([key,label])=>label+' '+data.counts[key]+'件').join(' ／ ') || '対象範囲で交通情報なし';
      stamp.textContent=time+'取得'+(data.stale?'・更新遅延':'');
      for(const item of data.incidents){
        const li=document.createElement('li');
        const location=[item.from,item.to].filter(x=>typeof x==='string'&&x).join(' → ');
        li.textContent=(labels[item.category]||'その他')+'：'+(location||'場所の詳細なし')+
          (Array.isArray(item.roads)&&item.roads.length?'（'+item.roads.join('・')+'）':'')+
          (typeof item.description==='string'&&item.description?' — '+item.description:'');
        list.append(li);
      }
      if(data.truncated){const li=document.createElement('li');li.textContent='詳細は先頭100件を表示しています。';list.append(li);}
      if (data.total === 0) {
        const li = document.createElement('li');
        li.textContent = '現在取得できた事故・通行止め・渋滞・工事の情報は0件です。最新の規制は公式情報も確認してください。';
        list.append(li);
      }
      details.hidden = false;

      expiry=setTimeout(unavailable,900000-age);
    }catch{unavailable();}
    finally{busy=false;setTimeout(()=>{button.disabled=false;},60000);}
  }
  button.addEventListener('click',update);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)update();});
  setInterval(()=>{if(!document.hidden)update();},300000);
  update();
  document.title='高松タクシー・シグナル v0.7w';
})();


