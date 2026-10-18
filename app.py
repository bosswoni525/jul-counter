"""
총공 구역별 인원 실시간 카운터 서버
- 관리자가 디시 게시글 최대 5개를 등록하면 서버에 저장됨
- 날짜를 고르면 그날 빌지([P] [U] 등)를 갤러리에서 찾아 자동 등록하고, 1분마다 새 빌지 확인
- 누구든 사이트에 들어오면 등록된 구역별 '줄' 인원을 실시간으로 봄
- 수정은 관리자 비밀번호(ADMIN_KEY 환경변수)를 아는 사람만 가능
"""
import os
import re
import json
import unicodedata
import html as htmllib
import time
import threading
from urllib.parse import urlparse, parse_qs, urlencode

import requests
from flask import Flask, jsonify, request

# ===== 설정 =====
KEYWORD = os.environ.get("KEYWORD", "줄")
# 쉼표로 여러 개 가능 (예: "신청,완료" → 신청 인원, 완료 인원을 따로 셈)
KEYWORDS = [k.strip() for k in KEYWORD.split(",") if k.strip()] or ["줄"]
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")  # Render 환경변수에 관리자 비밀번호 설정 (비우면 비밀번호 없음)
CACHE_SEC = 5      # 같은 글은 5초 동안 저장된 결과 재사용 (디시 요청 줄이기)
MAX_CACHE = 50     # 동시에 기억할 게시글 수
MAX_SLOTS = 5      # 최대 구역 수
SLOTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "slots.json")

# 빌지 자동 불러오기 설정
BILJI_GALLERY = os.environ.get("BILJI_GALLERY", "rsbakery")      # 빌지가 올라오는 미니 갤러리 id
# 빌지 올리는 계정 아이디 (highway1690 = 리센느음원총공팀, church4099 = Re:mine)
BILJI_UIDS = [u.strip() for u in os.environ.get("BILJI_UIDS", "highway1690,church4099").split(",") if u.strip()]
BILJI_SKIP_TAGS = {"총공", "공지", "필독", "안내"}
ZONE_ORDER = os.environ.get("ZONE_ORDER", "P,U,L,S,E").split(",")  # 카드 표시 순서                   # 빌지가 아닌 말머리
BILJI_RECHECK_SEC = 60                                            # 자동 모드에서 새 빌지 확인 간격
# ================

# 모두가 함께 보는 구역 설정 (서버에 저장)
# auto_date: "today" = 매일 오늘 빌지 자동 / "YYYY-MM-DD" = 그 날짜 고정 / "" = 직접 입력
_shared = {"slots": [], "contain": False, "version": 0, "auto_date": "today"}
_bilji_state = {"last": 0, "running": False}
_shared_lock = threading.Lock()


def _load_shared():
    try:
        with open(SLOTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        _shared.update(slots=data.get("slots", []), contain=bool(data.get("contain")),
                       version=int(data.get("version", 0)), auto_date=data.get("auto_date", "today"))
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


# 키워드별 추가 인정 패턴 (예: "총공완", "지각완"처럼 '완'으로 끝나면 완료로 인정)
KEYWORD_ALIASES = {
    "신청": [re.compile(r"참여")],   # "참여합니다"도 신청 인원으로 셈
    "완료": [re.compile(r"완$")],    # "총공완", "지각완"도 완료로 셈
}


def matches(text, k, contain):
    if (k in text) if contain else (text == k):
        return True
    return any(p.search(text) for p in KEYWORD_ALIASES.get(k, []))


def count_people(comments, contain, keywords=None):
    """키워드 단 사람 집계 (같은 사람은 1명으로)"""
    keywords = keywords or KEYWORDS
    people = {}
    for c in comments:
        if not is_real(c):
            continue  # 광고·삭제 댓글 제외
        text = clean(c.get("memo"))
        hit = any(matches(text, k, contain) for k in keywords)
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
    contain = True  # 키워드가 들어간 댓글은 무조건 셈 (예: "신청합니다", "저 신청이요")
    try:
        url, gal_id, post_no, gal_type = normalize_url(request.args.get("url"))
    except (ValueError, KeyError) as e:
        return jsonify(ok=False, error=str(e)), 400
    try:
        entry = get_comments_cached(url, gal_id, post_no, gal_type)
        # 키워드별로 따로 집계 (예: 신청 33명, 완료 12명)
        groups = []
        for k in KEYWORDS:
            ppl = count_people(entry["data"], contain, [k])
            groups.append({"kw": k, "count": len(ppl), "people": ppl})
        updated = time.strftime("%H:%M:%S", time.gmtime(entry["time"] + 9 * 3600))
        return jsonify(ok=True, url=url, groups=groups,
                       count=groups[0]["count"], people=groups[0]["people"],
                       total=sum(1 for c in entry["data"] if is_real(c)), updated=updated)
    except Exception as e:
        return jsonify(ok=False, error=f"디시에서 댓글을 못 가져왔어 ({e})"), 502


@app.route("/api/slots", methods=["GET"])
def get_slots():
    """지금 등록된 구역 목록 (누구나 볼 수 있음)"""
    maybe_recheck_bilji()
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
        _shared["auto_date"] = ""  # 직접 저장하면 빌지 자동 모드는 꺼짐
        _shared["version"] += 1
        _save_shared()
        return jsonify(ok=True, **_shared)


# ===== 빌지 자동 불러오기 =====
def kst_today():
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 9 * 3600))


def resolve_date(key):
    """'today'면 오늘 날짜(한국 시간)로 바꿈"""
    return kst_today() if key == "today" else key


def zone_name(tag):
    """[P] → [P]구역, [1구역] → 1구역"""
    return tag if "구역" in tag else f"[{tag}]구역"


def fetch_bilji(date):
    """갤러리 목록에서 date(YYYY-MM-DD)에 빌지 계정이 올린 [구역] 글 찾기"""
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    found = {}
    for page in range(1, 6):
        url = f"https://gall.dcinside.com/mini/board/lists/?id={BILJI_GALLERY}&page={page}"
        page_html = s.get(url, timeout=10, headers={"Referer": url}).text
        rows = re.findall(r'<tr class="ub-content[^"]*" data-no="(\d+)"(.*?)</tr>', page_html, re.S)
        if not rows:
            break
        older = False
        for no, body in rows:
            d = re.search(r'class="gall_date" title="([^"]+)"', body)
            uid = re.search(r'data-uid="([^"]*)"', body)
            t = re.search(r'class="gall_tit[^"]*">.*?<a [^>]*>(.*?)</a>', body, re.S)
            if not (d and uid and t):
                continue
            post_date = d.group(1)[:10]
            if post_date < date:
                older = True
                continue
            if post_date != date or uid.group(1) not in BILJI_UIDS:
                continue
            title = htmllib.unescape(re.sub(r"<[^>]*>", "", t.group(1))).strip()
            m = re.match(r"\[([^\]]+)\]", title)
            if not m or m.group(1).strip() in BILJI_SKIP_TAGS:
                continue
            # [𝑷] 같은 특수 글꼴 알파벳을 [P]로 통일
            tag = unicodedata.normalize("NFKC", m.group(1)).strip().upper()
            # 같은 구역 빌지가 여러 개면 가장 최신 글
            if tag not in found or int(no) > int(found[tag]["no"]):
                found[tag] = {"no": no, "tag": tag, "title": title}
        if older:
            break
    def order(x):
        t = x["tag"]
        return (ZONE_ORDER.index(t) if t in ZONE_ORDER else len(ZONE_ORDER), int(x["no"]))
    items = sorted(found.values(), key=order)[:MAX_SLOTS]
    return [{
        "name": zone_name(x["tag"]),
        "url": f"https://gall.dcinside.com/mini/board/view/?id={BILJI_GALLERY}&no={x['no']}",
        "title": x["title"],
    } for x in items]


def apply_bilji(key, slots):
    """찾은 빌지를 구역 목록에 반영 (바뀐 게 있을 때만)"""
    with _shared_lock:
        if _shared.get("auto_date") != key:
            return  # 그사이 관리자가 다른 설정으로 바꿨으면 무시
        old = [(x.get("name"), x.get("url")) for x in _shared["slots"] if x.get("url")]
        new = [(x["name"], x["url"]) for x in slots]
        if old != new:
            _shared["slots"] = slots
            _shared["version"] += 1
            _save_shared()


def maybe_recheck_bilji():
    """자동 모드면 1분마다 새 빌지가 올라왔는지 확인 (뒤에서 몰래 실행)"""
    key = _shared.get("auto_date")
    if not key or _bilji_state["running"] or time.time() - _bilji_state["last"] < BILJI_RECHECK_SEC:
        return
    _bilji_state["running"] = True
    _bilji_state["last"] = time.time()

    def run():
        try:
            slots = fetch_bilji(resolve_date(key))
            # 오늘 빌지가 아직 없으면 어제 빌지를 그대로 보여줌 (새 빌지 뜨면 교체)
            if slots:
                apply_bilji(key, slots)
        except Exception:
            pass
        finally:
            _bilji_state["running"] = False

    threading.Thread(target=run, daemon=True).start()


@app.route("/api/bilji", methods=["POST"])
def load_bilji():
    """선택한 날짜의 빌지를 찾아서 구역에 넣고, 이후 자동으로 새 빌지 확인"""
    body = request.get_json(silent=True) or {}
    if ADMIN_KEY and body.get("key") != ADMIN_KEY:
        return jsonify(ok=False, error="관리자 비밀번호가 틀렸어"), 403
    key = str(body.get("date") or "today")
    if key != "today" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", key):
        return jsonify(ok=False, error="날짜 형식이 이상해"), 400
    try:
        slots = fetch_bilji(resolve_date(key))
    except Exception as e:
        return jsonify(ok=False, error=f"디시 목록을 못 가져왔어 ({e})"), 502
    with _shared_lock:
        _shared["auto_date"] = key
        if slots:
            _shared["slots"] = slots
        _shared["version"] += 1
        _save_shared()
    _bilji_state["last"] = time.time()
    with _shared_lock:
        return jsonify(ok=True, found=len(slots), **_shared)


@app.route("/api/login", methods=["POST"])
def login():
    """관리자 비밀번호 확인만 함"""
    body = request.get_json(silent=True) or {}
    if not ADMIN_KEY:
        return jsonify(ok=True, open=True)  # 비밀번호 없이 누구나 수정 가능
    if body.get("key") == ADMIN_KEY:
        return jsonify(ok=True)
    return jsonify(ok=False, error="관리자 비밀번호가 틀렸어")


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
 .nums{display:flex;gap:22px;flex-wrap:wrap}
 .lbl{font-size:13px;color:#888;margin-top:6px}
 .multi .num{font-size:42px;margin:0}
 .multi .n1 .num{color:#2e7d5b}
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
 .copybtns{display:flex;gap:6px;margin-top:10px;flex-wrap:wrap}
 .cbtn{flex:1;min-width:0;padding:9px 6px;border:1px solid #c9cdea;border-radius:8px;background:#eef0fa;color:#3b4890;font-size:13px;cursor:pointer;white-space:nowrap}
 .cbtn.done{background:#3b4890;color:#fff}
 @media (max-width:600px){ .copybtns{flex-direction:column;gap:4px;margin-top:6px} .cbtn{font-size:12px;padding:7px 4px} }
 .bili{display:flex;gap:6px;margin-bottom:4px}
 .bili input{flex:1;min-width:0;padding:9px;border:1px solid #ccd;border-radius:8px;font-size:14px}
 .bili button{padding:9px 14px;border:0;border-radius:8px;background:#3b4890;color:#fff;font-size:13px;cursor:pointer;white-space:nowrap}
 @media (max-width:480px){.arow{grid-template-columns:1fr}}
 /* 폰: 카드를 작게 2열로 → 5개가 한 화면에 */
 @media (max-width:600px){
  .wrap{padding:12px 10px}
  h1{font-size:17px}
  .top{margin:6px 0 10px}
  .sum{padding:6px 14px;font-size:13px} .sum b{font-size:20px}
  .grid{grid-template-columns:1fr 1fr;gap:8px}
  .card{padding:10px 12px;border-radius:10px}
  .zone{font-size:14px}
  .num{font-size:30px;margin:2px 0 0}
  .nums{gap:12px} .lbl{font-size:11px;margin-top:2px}
  .multi .num{font-size:24px}
  .st{font-size:10px;margin-top:2px}
  details,a{font-size:11px} details{margin-top:2px}
 }
</style></head><body><div class="wrap">
 <h1>총공 구역별 인원 실시간 카운터</h1>
 <div class="st hide" id="autoinfo" style="text-align:center"></div>

 <div class="admin hide" id="admin">
  <h2>📅 날짜로 빌지 불러오기</h2>
  <div class="bili">
   <input type="date" id="bd">
   <button id="bload">이 날짜 빌지</button>
   <button id="btoday">매일 자동</button>
  </div>
  <div class="st" id="bmsg">매일 자동: 날짜가 바뀌면 그날 빌지를 알아서 찾아서 바꿔줘 (기본값). 이 날짜 빌지: 고른 날짜로 고정.</div>
  <h2 style="margin-top:16px">또는 직접 링크 입력 (최대 5개)</h2>
  <div id="rows"></div>
  <div class="abtn">
   <input type="checkbox" id="c" class="hide" checked>
   <button id="save">저장</button>
   <button class="gray hide" id="restore">이전 설정 불러오기</button>
   <button class="gray" id="viewmode">보기 모드로 돌아가기</button>
   <span id="amsg" class="st"></span>
  </div>
 </div>

 <div class="top"><div class="sum" id="total">전체 합계 <b>0</b>명</div></div>
 <div class="grid" id="grid"></div>
 <div class="empty hide" id="empty">아직 등록된 구역이 없어. 아래 관리자 버튼을 눌러 링크를 넣어줘.</div>
 <div class="foot"><button id="adminbtn">관리자</button></div>
</div>
<script>
const MAX=5;
const KWS=__KWS__;  // 서버의 KEYWORD 설정 (예: ["신청","완료"])
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
// 날짜 기본값 = 오늘(한국 시간)
$('bd').value=new Date(Date.now()+9*3600e3).toISOString().slice(0,10);
async function loadBilji(date){
  $('bmsg').className='st'; $('bmsg').textContent='빌지 찾는 중...';
  let d=await post('/api/bilji',{key:adminKey,date});
  if(!d.ok && /비밀번호/.test(d.error||'')){
    const k=prompt('관리자 비밀번호');
    if(k){ adminKey=k; store.set('jul_admin',k); d=await post('/api/bilji',{key:adminKey,date}); }
  }
  if(!d.ok){ $('bmsg').className='st err'; $('bmsg').textContent='오류: '+d.error; return; }
  const when = date==='today' ? '매일 자동 모드 켰어. ' : '';
  $('bmsg').textContent = when + (d.found
    ? `${d.found}개 구역 빌지를 불러왔어! 새 빌지가 올라오면 1분 안에 자동으로 추가돼.`
    : '아직 빌지가 없어. 올라오면 1분 안에 자동으로 들어와.');
  store.set('jul_backup',JSON.stringify({slots:d.slots,contain:d.contain}));
  apply(d,true);
}
$('bload').onclick=()=>loadBilji($('bd').value);
$('btoday').onclick=()=>loadBilji('today');

$('save').onclick=()=>{
  const slots=rows.map(r=>({name:r.querySelector('.an').value.trim(),url:r.querySelector('.au').value.trim()}));
  saveSlots(slots,$('c').checked);
};
// 관리자 모드 ↔ 보기 모드 전환 (비밀번호 없음)
function setMode(admin){
  $('admin').classList.toggle('hide',!admin);
  $('adminbtn').parentElement.classList.toggle('hide',admin);
  if(admin){ fillAdmin(); window.scrollTo(0,0); }
}
$('adminbtn').onclick=async()=>{
  // 서버에 비밀번호가 없거나, 이 기기에 저장된 비밀번호가 맞으면 바로 열림
  let d=await post('/api/login',{key:adminKey});
  if(!d.ok){
    const k=prompt('관리자 비밀번호');
    if(!k) return;
    d=await post('/api/login',{key:k});
    if(!d.ok){ alert(d.error||'비밀번호가 틀렸어'); return; }
    adminKey=k; store.set('jul_admin',k);
  }
  setMode(true);
};
$('viewmode').onclick=()=>setMode(false);

$('restore').onclick=()=>{
  try{ const b=JSON.parse(store.get('jul_backup')); saveSlots(b.slots||[],!!b.contain); }catch(e){}
};

// 폰이면 모바일 디시 주소로 → 디시 앱이 깔려 있으면 앱으로 열릴 수 있음
const isMobile=/Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
function postLink(url){
  if(!isMobile) return url;
  try{
    const u=new URL(url), id=u.searchParams.get('id'), no=u.searchParams.get('no');
    const kind=u.pathname.startsWith('/mini/')?'mini':'board';
    return `https://m.dcinside.com/${kind}/${id}/${no}`;
  }catch(e){ return url; }
}

// 디시 앱으로 열기 (디시 모바일 웹의 '디시앱' 버튼과 같은 dcapp:// 주소 사용)
const isAndroid=/Android/i.test(navigator.userAgent);
const isIOS=/iPhone|iPad|iPod/i.test(navigator.userAgent);
function openApp(url){
  const web=postLink(url);                       // https://m.dcinside.com/mini/id/no
  const path=web.replace('https://','');         // m.dcinside.com/mini/id/no
  if(isAndroid){
    // 앱이 없으면 모바일 웹으로 자동 이동
    location.href=`intent://${path}#Intent;scheme=dcapp;package=com.dcinside.app.android;S.browser_fallback_url=${encodeURIComponent(web)};end`;
  }else if(isIOS){
    const t=Date.now();
    location.href='dcapp://'+path;
    // 1.5초 안에 앱으로 안 넘어가면 모바일 웹으로
    setTimeout(()=>{ if(!document.hidden && Date.now()-t<3000) location.href=web; },1500);
  }else{
    window.open(url,'_blank');
  }
}

// ===== 댓글 문구 복사 버튼 =====
const COPY_TEXTS=['신청합니다','총공완료 했습니다'];
function copyText(t){
  // 앱 내 브라우저 등 clipboard API가 막힌 곳을 위해 예비 방법도 사용
  const fallback=()=>{
    const ta=document.createElement('textarea');
    ta.value=t; ta.setAttribute('readonly',''); ta.style.position='fixed'; ta.style.opacity='0';
    document.body.appendChild(ta); ta.select(); ta.setSelectionRange(0,t.length);
    let ok=false; try{ ok=document.execCommand('copy'); }catch(e){}
    ta.remove(); return ok;
  };
  if(navigator.clipboard && window.isSecureContext){
    return navigator.clipboard.writeText(t).then(()=>true).catch(fallback);
  }
  return Promise.resolve(fallback());
}
document.addEventListener('click',e=>{
  const b=e.target.closest('.cbtn'); if(!b) return;
  const t=b.dataset.t;
  copyText(t).then(ok=>{
    b.textContent= ok ? '복사됨!' : '복사 실패';
    b.classList.add('done');
    setTimeout(()=>{ b.textContent=t; b.classList.remove('done'); },1200);
  });
});

// ===== 보는 화면 =====
function render(){
  const list=shared.slots.map((s,i)=>({...s,i})).filter(s=>s.url);
  $('empty').classList.toggle('hide',list.length>0);
  $('grid').innerHTML=list.map(s=>`
    <div class="card" data-url="${esc(s.url)}">
      <div class="zone">${esc(s.name||('구역 '+(s.i+1)))}</div>
      <div class="nums ${KWS.length>1?'multi':''}">${KWS.map((k,j)=>`<div class="n${j}">${KWS.length>1?`<div class="lbl">${esc(k)}</div>`:''}<div class="num">-</div></div>`).join('')}</div>
      <div class="st">불러오는 중... (서버가 자고 있었으면 최대 1분)</div>
      ${isMobile
        ? `<a href="#" onclick="openApp('${esc(s.url)}');return false;">디시앱으로 열기</a> · <a href="${esc(postLink(s.url))}" target="_blank">웹으로 보기</a>`
        : `<a href="${esc(s.url)}" target="_blank">원본 게시물 보기</a>`}
      ${KWS.map(k=>`<details><summary>${KWS.length>1?esc(k)+' ':''}명단 보기</summary><ol></ol></details>`).join('')}
      <div class="copybtns">${COPY_TEXTS.map(t=>`<button class="cbtn" data-t="${esc(t)}">${esc(t)}</button>`).join('')}</div>
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
    const nums=card.querySelectorAll('.num'), ols=card.querySelectorAll('ol');
    (d.groups||[{count:d.count,people:d.people}]).forEach((g,j)=>{
      if(nums[j]) nums[j].textContent=g.count+'명';
      if(ols[j]) ols[j].innerHTML=g.people.map(p=>'<li>'+esc(p.name)+' <span class="t">'+esc(p.time)+'</span></li>').join('');
    });
    st.className='st';
    st.textContent='전체 댓글 '+d.total+'개 · 갱신 '+d.updated;
  }else{
    st.className='st err';
    st.textContent='오류: '+d.error;
  }
}

function sum(){
  const tot=KWS.map(()=>0);
  shared.slots.forEach(s=>{
    const d=s.url&&counts[s.url];
    if(d&&d.ok) (d.groups||[{count:d.count}]).forEach((g,j)=>{ tot[j]+=g.count; });
  });
  $('total').innerHTML = KWS.length>1
    ? KWS.map((k,j)=>`${esc(k)} 합계 <b>${tot[j]}</b>명`).join(' · ')
    : `전체 합계 <b>${tot[0]}</b>명`;
}

function apply(d,force){
  const changed=force||d.version!==shared.version;
  shared={slots:d.slots||[],contain:!!d.contain,version:d.version,auto_date:d.auto_date||''};
  const ai=$('autoinfo');
  if(shared.auto_date==='today'){
    ai.textContent='오늘 빌지 자동 연동 중 · 날짜가 바뀌고 새 빌지가 올라오면 자동으로 바뀌어';
    ai.classList.remove('hide');
  }else if(shared.auto_date){
    const [y,m,dd]=shared.auto_date.split('-');
    ai.textContent=`${+m}월 ${+dd}일 빌지 고정 · 새 빌지가 올라오면 자동으로 추가돼`;
    ai.classList.remove('hide');
  }else ai.classList.add('hide');
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
    return PAGE.replace("__KWS__", json.dumps(KEYWORDS, ensure_ascii=False)).replace("__KW__", "/".join(KEYWORDS))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
