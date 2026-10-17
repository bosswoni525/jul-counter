"""
디시 게시물 '줄' 인원 실시간 중계 서버
- 서버가 디시 댓글을 대신 읽어와서 '줄' 단 사람 수를 셈
- 접속한 사람들은 이 서버의 페이지를 보면 됨 (5초마다 자동 갱신)
"""
import os
import re
import time
import threading
from urllib.parse import urlparse, parse_qs

import requests
from flask import Flask, jsonify, request

# ===== 설정 =====
# Render 환경변수 POST_URL 로 바꿀 수 있음
POST_URL = os.environ.get(
    "POST_URL",
    "https://gall.dcinside.com/mini/board/view/?id=rsbakery&no=17&page=1",
)
KEYWORD = os.environ.get("KEYWORD", "줄")
CACHE_SEC = 5  # 디시에 너무 자주 요청하지 않도록 5초 동안은 저장된 결과 재사용
# ================

app = Flask(__name__)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

_cache = {"time": 0, "data": None}
_lock = threading.Lock()


def parse_post_url(url):
    """게시물 주소에서 갤러리 id, 글 번호, 갤러리 종류를 뽑아냄"""
    u = urlparse(url)
    q = parse_qs(u.query)
    gal_id = q["id"][0]
    post_no = q["no"][0]
    if u.path.startswith("/mini/"):
        gal_type = "MI"
    elif u.path.startswith("/mgallery/"):
        gal_type = "M"
    else:
        gal_type = "G"
    return gal_id, post_no, gal_type


def fetch_comments(url):
    """게시물의 모든 댓글 가져오기"""
    gal_id, post_no, gal_type = parse_post_url(url)
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


def get_comments_cached():
    with _lock:
        if _cache["data"] is None or time.time() - _cache["time"] > CACHE_SEC:
            _cache["data"] = fetch_comments(POST_URL)
            _cache["time"] = time.time()
        return _cache["data"]


@app.route("/api/count")
def api_count():
    contain = request.args.get("contain") == "1"
    try:
        comments = get_comments_cached()
        people = count_people(comments, contain)
        return jsonify(ok=True, count=len(people), people=people,
                       total=len(comments), updated=time.strftime("%H:%M:%S", time.gmtime(_cache["time"] + 9 * 3600)))
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


PAGE = """<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>줄 카운터</title>
<style>
 body{margin:0;font-family:sans-serif;background:#f4f5fb;color:#222}
 .box{max-width:480px;margin:24px auto;background:#fff;border-radius:14px;padding:20px;box-shadow:0 4px 16px rgba(0,0,0,.08)}
 h1{font-size:18px;color:#3b4890;margin:0 0 4px}
 .num{font-size:64px;font-weight:bold;margin:8px 0}
 .st{font-size:12px;color:#888}
 ol{padding-left:22px} li{margin:4px 0} .t{color:#999;font-size:12px}
 a{color:#3b4890;font-size:13px}
</style></head><body><div class="box">
 <h1>'__KW__' 인원 실시간 중계</h1>
 <a href="__URL__" target="_blank">원본 게시물 보기</a>
 <div class="num" id="n">-</div>
 <label><input type="checkbox" id="c"> '__KW__' 포함된 댓글도 인정</label>
 <div class="st" id="s">불러오는 중... (첫 접속은 최대 1분 걸릴 수 있음)</div>
 <ol id="l"></ol>
</div>
<script>
async function load(){
  try{
    const c=document.getElementById('c').checked?1:0;
    const r=await fetch('/api/count?contain='+c);
    const d=await r.json();
    if(!d.ok) throw new Error(d.error);
    document.getElementById('n').textContent=d.count+'명';
    document.getElementById('s').textContent='전체 댓글 '+d.total+'개 · 갱신 '+d.updated;
    document.getElementById('l').innerHTML=d.people.map(p=>
      '<li>'+p.name.replace(/</g,'&lt;')+' <span class="t">'+p.time+'</span></li>').join('');
  }catch(e){document.getElementById('s').textContent='오류: '+e.message;}
}
document.getElementById('c').onchange=load;
load(); setInterval(load,5000);
</script></body></html>"""


@app.route("/")
def index():
    return PAGE.replace("__KW__", KEYWORD).replace("__URL__", POST_URL)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
