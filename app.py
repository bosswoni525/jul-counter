"""
디시 게시물 '줄' 인원 실시간 중계 서버
- 페이지에 게시글 링크를 붙이면 그 글에 '줄' 단 사람 수를 실시간으로 보여줌
- 링크가 들어간 주소를 공유하면 다른 사람도 같은 화면을 볼 수 있음
"""
import os
import re
import time
import threading
from urllib.parse import urlparse, parse_qs, urlencode

import requests
from flask import Flask, jsonify, request

# ===== 설정 =====
KEYWORD = os.environ.get("KEYWORD", "줄")
CACHE_SEC = 5      # 같은 글은 5초 동안 저장된 결과 재사용 (디시 요청 줄이기)
MAX_CACHE = 50     # 동시에 기억할 게시글 수
# ================

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
    return re.sub(r"[^\w]", "", text).replace("_", "")


def count_people(comments, contain):
    """'줄' 단 사람 집계 (같은 사람은 1명으로)"""
    people = {}
    for c in comments:
        if c.get("nicktype") == "COMMENT_BOY" or c.get("name") == "댓글돌이":
            continue  # 광고 댓글 제외
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
                       total=len(entry["data"]), updated=updated)
    except Exception as e:
        return jsonify(ok=False, error=f"디시에서 댓글을 못 가져왔어 ({e})"), 502


PAGE = """<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>줄 카운터</title>
<style>
 *{box-sizing:border-box}
 body{margin:0;font-family:sans-serif;background:#f4f5fb;color:#222}
 .box{max-width:480px;margin:24px auto;background:#fff;border-radius:14px;padding:20px;box-shadow:0 4px 16px rgba(0,0,0,.08)}
 h1{font-size:18px;color:#3b4890;margin:0 0 12px}
 form{display:flex;gap:6px;margin-bottom:12px}
 input[type=text]{flex:1;min-width:0;padding:10px;border:1px solid #ccd;border-radius:8px;font-size:14px}
 button{padding:10px 14px;border:0;border-radius:8px;background:#3b4890;color:#fff;font-size:14px;cursor:pointer}
 .num{font-size:64px;font-weight:bold;margin:8px 0}
 .st{font-size:12px;color:#888;margin-top:6px}
 .err{color:#c33}
 ol{padding-left:22px} li{margin:4px 0} .t{color:#999;font-size:12px}
 a{color:#3b4890;font-size:13px}
 #share{font-size:12px;margin-left:6px}
 .hide{display:none}
</style></head><body><div class="box">
 <h1>'__KW__' 인원 실시간 카운터</h1>
 <form id="f">
  <input type="text" id="u" placeholder="디시 게시글 링크 붙여넣기">
  <button>확인</button>
 </form>
 <div id="res" class="hide">
  <a id="orig" target="_blank">원본 게시물 보기</a>
  <a href="#" id="share">이 화면 링크 복사</a>
  <div class="num" id="n">-</div>
  <label><input type="checkbox" id="c"> '__KW__' 포함된 댓글도 인정</label>
  <div class="st" id="s"></div>
  <ol id="l"></ol>
 </div>
 <div class="st" id="msg">게시글 링크를 붙이고 확인을 누르면 5초마다 자동으로 갱신돼.</div>
</div>
<script>
const $=id=>document.getElementById(id);
let target='', timer=null;

function start(url){
  target=url.trim();
  if(!target) return;
  $('u').value=target;
  // 주소창에 링크 저장 → 이 주소를 공유하면 같은 글이 바로 뜸
  history.replaceState(null,'','?url='+encodeURIComponent(target));
  $('res').classList.remove('hide');
  $('n').textContent='-'; $('l').innerHTML='';
  $('s').textContent='불러오는 중... (서버가 자고 있었으면 최대 1분)';
  $('msg').classList.add('hide');
  clearInterval(timer); load(); timer=setInterval(load,5000);
}

async function load(){
  if(!target) return;
  try{
    const c=$('c').checked?1:0;
    const r=await fetch('/api/count?contain='+c+'&url='+encodeURIComponent(target));
    const d=await r.json();
    if(!d.ok) throw new Error(d.error);
    $('orig').href=d.url;
    $('n').textContent=d.count+'명';
    $('s').className='st';
    $('s').textContent='전체 댓글 '+d.total+'개 · 갱신 '+d.updated;
    $('l').innerHTML=d.people.map(p=>
      '<li>'+p.name.replace(/</g,'&lt;')+' <span class="t">'+p.time+'</span></li>').join('');
  }catch(e){ $('s').className='st err'; $('s').textContent='오류: '+e.message; }
}

$('f').onsubmit=e=>{e.preventDefault(); start($('u').value);};
$('c').onchange=load;
$('share').onclick=e=>{
  e.preventDefault();
  navigator.clipboard.writeText(location.href).then(()=>{$('share').textContent='복사됨!';
    setTimeout(()=>$('share').textContent='이 화면 링크 복사',1500);});
};
// 주소에 ?url= 이 있으면 바로 시작
const init=new URLSearchParams(location.search).get('url');
if(init) start(init);
</script></body></html>"""


@app.route("/")
def index():
    return PAGE.replace("__KW__", KEYWORD)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
