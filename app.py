"""
총공 구역별 인원 실시간 카운터 서버
- 관리자가 디시 게시글 최대 5개를 등록하면 서버에 저장됨
- 누구든 사이트에 들어오면 등록된 구역별 '줄' 인원을 실시간으로 봄
- 수정은 누구나 가능 (ADMIN_KEY 환경변수를 넣으면 비밀번호 필요)
"""
import os
import re
import json
import time
import threading
from urllib.parse import urlparse, parse_qs, urlencode

import requests
from flask import Flask, jsonify, request

# ===== 설정 =====
KEYWORD = os.environ.get("KEYWORD", "줄")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")  # 비워두면 비밀번호 없이 누구나 수정 가능
CACHE_SEC = 5      # 같은 글은 5초 동안 저장된 결과 재사용 (디시 요청 줄이기)
MAX_CACHE = 50     # 동시에 기억할 게시글 수
MAX_SLOTS = 5      # 최대 구역 수
SLOTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "slots.json")
# ================

# 모두가 함께 보는 구역 설정 (서버에 저장)
_shared = {"slots": [], "contain": False, "version": 0}
_shared_lock = threading.Lock()


def _load_shared():
    try:
        with open(SLOTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        _shared.update(slots=data.get("slots", []), contain=bool(data.get("contain")),
                       version=int(data.get("version", 0)))
    except (OSError, ValueError):
        pass


def _save_shared():
    try:
        with open(SLOTS_FILE, "w", encoding="utf-8") as f:
            json.dump(_shared, f, ensure_ascii=False)
    except OSError:
        pass


_load_shared()

app = Flask(__name__)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

_cache = {}  # url -> {"time": ..., "data": ...}
_lock = threading.Lock()


def normalize_url(raw):
    """
    붙여넣은 링크에서 갤러리 id, 글 번호, 갤러리 종류를 뽑아서
    PC용 정식 주소로 바꿈. 디시 주소가 아니면 에러.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("게시글 링크를 입력해줘")
    if not raw.startswith("http"):
        raw = "https://" + raw
    u = urlparse(raw)
    host = u.netloc.lower()

    # PC 주소: gall.dcinside.com/(mini|mgallery)/board/view/?id=..&no=..
    if host == "gall.dcinside.com":
        q = parse_qs(u.query)
        if "id" not in q or "no" not in q:
            raise ValueError("링크에 id 또는 no가 없어. 게시글 주소를 그대로 붙여줘")
        gal_id, post_no = q["id"][0], q["no"][0]
        if u.path.startswith("/mini/"):
            gal_type = "MI"
        elif u.path.startswith("/mgallery/"):
            gal_type = "M"
        else:
            gal_type = "G"

    # 모바일 주소: m.dcinside.com/board/{id}/{no} 또는 m.dcinside.com/mini/{id}/{no}
    elif host == "m.dcinside.com":
        m = re.match(r"^/(board|mini)/([^/]+)/(\d+)", u.path)
        if not m:
            raise ValueError("모바일 게시글 주소 형식이 아니야")
        kind, gal_id, post_no = m.groups()
        gal_type = "MI" if kind == "mini" else "G"
    else:
        raise ValueError("디시인사이드 게시글 링크만 가능해")

    if not re.fullmatch(r"[A-Za-z0-9_]+", gal_id) or not post_no.isdigit():
        raise ValueError("링크 형식이 이상해")

    prefix = {"MI": "/mini", "M": "/mgallery", "G": ""}[gal_type]
    url = f"https://gall.dcinside.com{prefix}/board/view/?" + urlencode({"id": gal_id, "no": post_no})
    return url, gal_id, post_no, gal_type


def fetch_comments(url, gal_id, post_no, gal_type):
    """게시물의 모든 댓글 가져오기"""
    s = requests.Session()
    s.headers.update({"User-Agent": UA})

    # 1) 게시물 페이지를 먼저 열어서 쿠키와 e_s_n_o 값 얻기
    html = s.get(url, timeout=10).text
    m = re.search(r'id="e_s_n_o"[^>]*value="([^"]*)"', html) or re.search(
        r'name="e_s_n_o"[^>]*value="([^"]*)"', html
    )
    esno = m.group(1) if m else ""

    # 2) 댓글 API 페이지별로 호출
    comments = {}
    for page in range(1, 51):
        r = s.post(
            "https://gall.dcinside.com/board/comment/",
            data={
                "id": gal_id, "no": post_no, "cmt_id": gal_id, "cmt_no": post_no,
                "focus_cno": "", "focus_pno": "", "e_s_n_o": esno,
                "comment_page": str(page), "sort": "", "prevCnt": "",
                "board_type": "", "_GALLTYPE_": gal_type,
            },
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": url,
                "Origin": "https://gall.dcinside.com",
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json() or {}
        items = data.get("comments") or []
        added = 0
        for c in items:
            if c.get("no") not in comments:
                comments[c.get("no")] = c
                added += 1
        if not items or added == 0:
            break
        if len(comments) >= int(data.get("total_cnt") or 0):
            break

    # 먼저 단 순서로 정렬
    return sorted(comments.values(), key=lambda c: int(c.get("no") or 0))


def clean(text):
    """HTML 태그, 공백, 기호 제거"""
    text = re.sub(r"<[^>]*>", "", str(text or ""))
    # 디시 앱에서 달면 붙는 "- dc App" 꼬리표 제거
    text = re.sub(r"\s*-\s*dc\s*(official\s*)?app\s*$", "", text, flags=re.I)
    return re.sub(r"[^\w]", "", text).replace("_", "")


def is_real(c):
    """광고(댓글돌이)와 삭제된 댓글이 아니면 True"""
    if c.get("nicktype") == "COMMENT_BOY" or c.get("name") == "댓글돌이":
        return False
    if str(c.get("del_yn", "")).upper() == "Y" or "삭제된 댓글" in str(c.get("memo", "")):
        return False
    return True


def count_people(comments, contain):
    """'줄' 단 사람 집계 (같은 사람은 1명으로)"""
    people = {}
    for c in comments:
        if not is_real(c):
            continue  # 광고·삭제 댓글 제외
        text = clean(c.get("memo"))
        hit = KEYWORD in text if contain else text == KEYWORD
        if not hit:
            continue
        uid = c.get("user_id")
        key = f"id:{uid}" if uid else f"ip:{c.get('name')}|{c.get('ip')}"
        if key not in people:
            name = c.get("name") or "?"
            if not uid and c.get("ip"):
                name += f" ({c.get('ip')})"
            people[key] = {"name": name, "time": c.get("reg_date", "")}
    return list(people.values())


def get_comments_cached(url, gal_id, post_no, gal_type):
    with _lock:
        hit = _cache.get(url)
        if hit and time.time() - hit["time"] <= CACHE_SEC:
            return hit
    data = fetch_comments(url, gal_id, post_no, gal_type)
    entry = {"time": time.time(), "data": data}
    with _lock:
        _cache[url] = entry
        if len(_cache) > MAX_CACHE:  # 오래된 것부터 정리
            oldest = min(_cache, key=lambda k: _cache[k]["time"])
            _cache.pop(oldest, None)
    return entry


@app.route("/api/count")
def api_count():
    contain = request.args.get("contain") == "1"
    try:
        url, gal_id, post_no, gal_type = normalize_url(request.args.get("url"))
    except (ValueError, KeyError) as e:
        return jsonify(ok=False, error=str(e)), 400
    try:
        entry = get_comments_cached(url, gal_id, post_no, gal_type)
        people = count_people(entry["data"], contain)
        updated = time.strftime("%H:%M:%S", time.gmtime(entry["time"] + 9 * 3600))
        return jsonify(ok=True, url=url, count=len(people), people=people,
                       total=sum(1 for c in entry["data"] if is_real(c)), updated=updated)
    except Exception as e:
        return jsonify(ok=False, error=f"디시에서 댓글을 못 가져왔어 ({e})"), 502


@app.route("/api/slots", methods=["GET"])
def get_slots():
    """지금 등록된 구역 목록 (누구나 볼 수 있음)"""
    with _shared_lock:
        return jsonify(ok=True, **_shared)


@app.route("/api/slots", methods=["POST"])
def set_slots():
    """구역 목록 저장 (관리자 비밀번호 필요)"""
    body = request.get_json(silent=True) or {}
    if ADMIN_KEY and body.get("key") != ADMIN_KEY:
        return jsonify(ok=False, error="관리자 비밀번호가 틀렸어"), 403
    slots = []
    for s in (body.get("slots") or [])[:MAX_SLOTS]:
        raw = str((s or {}).get("url", "")).strip()
        name = str((s or {}).get("name", "")).strip()[:30]
        if not raw:
            slots.append({"url": "", "name": name})
            continue
        try:
            url = normalize_url(raw)[0]
        except (ValueError, KeyError) as e:
            return jsonify(ok=False, error=f"{name or '구역'}: {e}"), 400
        slots.append({"url": url, "name": name})
    with _shared_lock:
        _shared["slots"] = slots
        _shared["contain"] = bool(body.get("contain"))
        _shared["version"] += 1
        _save_shared()
        return jsonify(ok=True, **_shared)


@app.route("/api/login", methods=["POST"])
def login():
    """관리자 비밀번호 확인만 함"""
    body = request.get_json(silent=True) or {}
    if not ADMIN_KEY:
        return jsonify(ok=True, open=True)  # 비밀번호 없이 누구나 수정 가능
    return jsonify(ok=body.get("key") == ADMIN_KEY)


PAGE = """<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>총공 구역별 인원 실시간 카운터</title>
<style>
 *{box-sizing:border-box}
 body{margin:0;font-family:sans-serif;background:#f4f5fb;color:#222}
 .wrap{max-width:1100px;margin:0 auto;padding:20px 16px}
 h1{font-size:22px;color:#3b4890;margin:0 0 6px;text-align:center}
 .top{display:flex;justify-content:center;margin:10px 0 18px}
 .sum{background:#3b4890;color:#fff;border-radius:10px;padding:8px 18px;font-size:15px}
 .sum b{font-size:24px}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px}
 .card{background:#fff;border-radius:14px;padding:16px;box-shadow:0 4px 16px rgba(0,0,0,.08)}
 .zone{font-size:17px;font-weight:bold;color:#3b4890}
 .num{font-size:52px;font-weight:bold;margin:4px 0 0}
 .st{font-size:12px;color:#888;margin-top:4px}
 .err{color:#c33}
 .empty{text-align:center;color:#888;padding:40px 0}
 details{margin-top:6px;font-size:13px} summary{cursor:pointer;color:#3b4890}
 ol{padding-left:22px;margin:6px 0} li{margin:3px 0} .t{color:#999;font-size:11px}
 a{color:#3b4890;font-size:12px}
 .foot{text-align:center;margin-top:24px}
 .foot button{background:none;border:0;color:#aaa;font-size:12px;cursor:pointer;text-decoration:underline}
 /* 관리자 패널 */
 .admin{background:#fff;border:2px solid #3b4890;border-radius:14px;padding:16px;margin-bottom:18px}
 .admin h2{font-size:15px;margin:0 0 10px;color:#3b4890}
 .arow{display:grid;grid-template-columns:110px 1fr;gap:6px;margin-bottom:6px}
 .admin input[type=text]{width:100%;padding:9px;border:1px solid #ccd;border-radius:8px;font-size:14px}
 .abtn{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:10px;font-size:13px}
 .abtn button{padding:9px 14px;border:0;border-radius:8px;background:#3b4890;color:#fff;font-size:13px;cursor:pointer}
 .abtn button.gray{background:#e3e5f0;color:#444}
 .hide{display:none}
 @media (max-width:480px){.arow{grid-template-columns:1fr}}
</style></head><body><div class="wrap">
 <h1>총공 구역별 인원 실시간 카운터</h1>

 <div class="admin" id="admin">
  <h2>게시글 링크 입력 (최대 5개) · 저장하면 모든 사람 화면에 반영돼</h2>
  <div id="rows"></div>
  <div class="abtn">
   <label><input type="checkbox" id="c"> '__KW__' 포함된 댓글도 인정</label>
   <button id="save">저장</button>
   <button class="gray hide" id="restore">이전 설정 불러오기</button>
   <span id="amsg" class="st"></span>
  </div>
 </div>

 <div class="top"><div class="sum">전체 합계 <b id="total">0</b>명</div></div>
 <div class="grid" id="grid"></div>
 <div class="empty hide" id="empty">위 칸에 게시글 링크를 넣고 저장을 눌러줘.</div>
</div>
<script>
const MAX=5;
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]));
const store={
  get:k=>{try{return localStorage.getItem(k);}catch(e){return null;}},
  set:(k,v)=>{try{localStorage.setItem(k,v);}catch(e){}},
  del:k=>{try{localStorage.removeItem(k);}catch(e){}},
};

let shared={slots:[],contain:false,version:-1};
let adminKey=store.get('jul_admin')||'';
const counts={};   // url -> 결과

// ===== 관리자 입력칸 5줄 만들기 =====
for(let i=0;i<MAX;i++){
  const r=document.createElement('div');
  r.className='arow';
  r.innerHTML=`<input type="text" class="an" placeholder="구역 ${i+1} 이름"><input type="text" class="au" placeholder="디시 게시글 링크">`;
  $('rows').appendChild(r);
}
const rows=[...$('rows').children];

function fillAdmin(){
  rows.forEach((r,i)=>{
    const s=shared.slots[i]||{};
    r.querySelector('.an').value=s.name||'';
    r.querySelector('.au').value=s.url||'';
  });
  $('c').checked=!!shared.contain;
  const backup=store.get('jul_backup');
  $('restore').classList.toggle('hide',!(backup && !shared.slots.some(s=>s.url)));
}


async function post(path,body){
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  return r.json();
}


async function saveSlots(slots,contain){
  $('amsg').className='st'; $('amsg').textContent='저장 중...';
  let d=await post('/api/slots',{key:adminKey,slots,contain});
  if(!d.ok && /비밀번호/.test(d.error||'')){
    const k=prompt('비밀번호');
    if(k){ adminKey=k; store.set('jul_admin',k); d=await post('/api/slots',{key:adminKey,slots,contain}); }
  }
  if(!d.ok){ $('amsg').className='st err'; $('amsg').textContent='오류: '+d.error; return; }
  store.set('jul_backup',JSON.stringify({slots:d.slots,contain:d.contain}));
  $('amsg').textContent='저장됨! 모든 사람 화면에 반영돼';
  setTimeout(()=>$('amsg').textContent='',2500);
  apply(d,true);
}
$('save').onclick=()=>{
  const slots=rows.map(r=>({name:r.querySelector('.an').value.trim(),url:r.querySelector('.au').value.trim()}));
  saveSlots(slots,$('c').checked);
};
$('restore').onclick=()=>{
  try{ const b=JSON.parse(store.get('jul_backup')); saveSlots(b.slots||[],!!b.contain); }catch(e){}
};

// ===== 보는 화면 =====
function render(){
  const list=shared.slots.map((s,i)=>({...s,i})).filter(s=>s.url);
  $('empty').classList.toggle('hide',list.length>0);
  $('grid').innerHTML=list.map(s=>`
    <div class="card" data-url="${esc(s.url)}">
      <div class="zone">${esc(s.name||('구역 '+(s.i+1)))}</div>
      <div class="num">-</div>
      <div class="st">불러오는 중... (서버가 자고 있었으면 최대 1분)</div>
      <a href="${esc(s.url)}" target="_blank">원본 게시물 보기</a>
      <details><summary>명단 보기</summary><ol></ol></details>
    </div>`).join('');
  list.forEach(s=>{ if(counts[s.url]) paint(s.url); });
  sum();
}

function paint(url){
  const card=[...$('grid').children].find(c=>c.dataset.url===url);
  const d=counts[url];
  if(!card||!d) return;
  const st=card.querySelector('.st');
  if(d.ok){
    card.querySelector('.num').textContent=d.count+'명';
    st.className='st';
    st.textContent='전체 댓글 '+d.total+'개 · 갱신 '+d.updated;
    card.querySelector('ol').innerHTML=d.people.map(p=>'<li>'+esc(p.name)+' <span class="t">'+esc(p.time)+'</span></li>').join('');
  }else{
    st.className='st err';
    st.textContent='오류: '+d.error;
  }
}

function sum(){
  $('total').textContent=shared.slots.reduce((a,s)=>a+((s.url&&counts[s.url]&&counts[s.url].ok)?counts[s.url].count:0),0);
}

function apply(d,force){
  const changed=force||d.version!==shared.version;
  shared={slots:d.slots||[],contain:!!d.contain,version:d.version};
  if(changed){ render(); fillAdmin(); loadCounts(); }
}

async function loadCounts(){
  const urls=[...new Set(shared.slots.map(s=>s.url).filter(Boolean))];
  await Promise.all(urls.map(async url=>{
    try{
      const r=await fetch('/api/count?contain='+(shared.contain?1:0)+'&url='+encodeURIComponent(url));
      counts[url]=await r.json();
    }catch(e){ counts[url]={ok:false,error:e.message}; }
    paint(url);
  }));
  sum();
}

async function tick(){
  try{
    const r=await fetch('/api/slots');
    const d=await r.json();
    if(d.ok){
      const changed=d.version!==shared.version;
      apply(d,false);
      if(!changed) loadCounts();
    }
  }catch(e){}
}

tick();
setInterval(tick,5000);
</script></body></html>
"""


@app.route("/")
def index():
    return PAGE.replace("__KW__", KEYWORD)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
