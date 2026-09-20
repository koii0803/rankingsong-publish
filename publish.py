"""깃허브 액션 발행기. 예약표.json 에서 publish_at(KST) 지난 '대기' 건을 유튜브·페북·인스타에 그 자리에서 게시한다.
앱 예약 기능 안 씀 — 시각은 예약표가 정하고, 게시는 이 스크립트가 깨어난 시점(30분 간격)에 함.
영상은 R2 공개 URL. 유튜브는 받아서 올리고, 페북·인스타는 URL을 넘긴다.
전부 끝난 편은 24시간 뒤 R2에서 지운다(R2 시크릿 있을 때).
Secrets: YT_CLIENT_SECRET, YT_TOKEN_KO/JA/EN(토큰 json 문자열), META_USER_TOKEN,
         R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, CLOUDFLARE_ACCOUNT_ID_2"""
import os, sys, json, time, datetime as dt, tempfile, urllib.request
import requests

KST = dt.timezone(dt.timedelta(hours=9))
GRAPH = "https://graph.facebook.com/v21.0"
Q = "예약표.json"
LOG = "발행기록.jsonl"
PAGES = {"ko": "1399063196619050", "en": "1397363193453435"}  # ja: 페이지 생기면
CLEANUP_H = 24


def now():
    return dt.datetime.now(KST)


def log(**kw):
    kw["at"] = now().isoformat(timespec="seconds")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(kw, ensure_ascii=False) + "\n")
    print(json.dumps(kw, ensure_ascii=False))


# ── 유튜브 ──────────────────────────────────────────────────────────
def yt_client(lang):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    tok = os.environ.get(f"YT_TOKEN_{lang.upper()}")
    if not tok:
        raise RuntimeError(f"YT_TOKEN_{lang.upper()} 시크릿 없음")
    c = Credentials.from_authorized_user_info(json.loads(tok))
    return build("youtube", "v3", credentials=c)


def yt_publish(it):
    from googleapiclient.http import MediaFileUpload
    yt = yt_client(it["lang"])
    fp = os.path.join(tempfile.gettempdir(), it["ep"] + ".mp4")
    urllib.request.urlretrieve(it["video_url"], fp)
    body = {"snippet": {"title": it["title"][:100], "description": it["description"], "categoryId": "22", "defaultLanguage": it["lang"]},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    req = yt.videos().insert(part="snippet,status", body=body, media_body=MediaFileUpload(fp, chunksize=8 * 1024 * 1024, resumable=True))
    res = None
    while res is None:
        _, res = req.next_chunk()
    vid = res["id"]
    cid = None
    if it.get("comment"):
        try:
            cid = yt.commentThreads().insert(part="snippet", body={"snippet": {"videoId": vid, "topLevelComment": {"snippet": {"textOriginal": it["comment"]}}}}).execute()["id"]
        except Exception as e:
            log(ep=it["ep"], lang=it["lang"], platform="youtube", warn="댓글 실패 " + str(e)[:200])
    os.remove(fp)
    return {"url": f"https://youtube.com/shorts/{vid}", "video_id": vid, "comment_id": cid}


# ── 메타 ────────────────────────────────────────────────────────────
def page_token(lang):
    ut = os.environ.get("META_USER_TOKEN")
    if not ut:
        raise RuntimeError("META_USER_TOKEN 시크릿 없음")
    pid = PAGES.get(lang)
    if not pid:
        raise RuntimeError(f"{lang} 페북 페이지 없음")
    r = requests.get(f"{GRAPH}/{pid}", params={"fields": "access_token,instagram_business_account", "access_token": ut}, timeout=30).json()
    if "error" in r:
        raise RuntimeError(f"페이지 토큰 {r['error']}")
    return pid, r["access_token"], (r.get("instagram_business_account") or {}).get("id")


def fb_publish(it):
    pid, ptok, _ = page_token(it["lang"])
    s = requests.post(f"{GRAPH}/{pid}/video_reels", data={"upload_phase": "start", "access_token": ptok}, timeout=60).json()
    if "video_id" not in s:
        raise RuntimeError(f"fb start {s}")
    vid = s["video_id"]
    u = requests.post(f"https://rupload.facebook.com/video-upload/v21.0/{vid}", headers={"Authorization": f"OAuth {ptok}", "file_url": it["video_url"]}, timeout=180).json()
    if not u.get("success"):
        raise RuntimeError(f"fb upload {u}")
    for _ in range(60):
        st = requests.get(f"{GRAPH}/{vid}", params={"fields": "status", "access_token": ptok}, timeout=30).json().get("status", {})
        if st.get("uploading_phase", {}).get("status") == "complete":
            break
        time.sleep(5)
    f = requests.post(f"{GRAPH}/{pid}/video_reels", data={"upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED", "description": it["text"], "access_token": ptok}, timeout=60).json()
    if not f.get("success"):
        raise RuntimeError(f"fb finish {f}")
    return {"video_id": vid, "url": f"https://www.facebook.com/reel/{vid}"}


def ig_publish(it):
    _, ptok, igid = page_token(it["lang"])
    if not igid:
        raise RuntimeError("페이지에 인스타 연결 없음")
    c = requests.post(f"{GRAPH}/{igid}/media", data={"media_type": "REELS", "video_url": it["video_url"], "caption": it["caption"], "share_to_feed": "true", "access_token": ptok}, timeout=60).json()
    if "id" not in c:
        raise RuntimeError(f"ig media {c}")
    for _ in range(60):
        st = requests.get(f"{GRAPH}/{c['id']}", params={"fields": "status_code", "access_token": ptok}, timeout=30).json()
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") == "ERROR":
            raise RuntimeError(f"ig 처리 실패 {st}")
        time.sleep(5)
    p = requests.post(f"{GRAPH}/{igid}/media_publish", data={"creation_id": c["id"], "access_token": ptok}, timeout=60).json()
    if "id" not in p:
        raise RuntimeError(f"ig publish {p}")
    return {"media_id": p["id"]}


# ── R2 정리 ─────────────────────────────────────────────────────────
def r2_delete(key):
    import boto3
    e = {k: os.environ.get(k) for k in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "CLOUDFLARE_ACCOUNT_ID_2")}
    if not all(e.values()):
        return False
    c = boto3.client("s3", endpoint_url=f"https://{e['CLOUDFLARE_ACCOUNT_ID_2']}.r2.cloudflarestorage.com", aws_access_key_id=e["R2_ACCESS_KEY_ID"], aws_secret_access_key=e["R2_SECRET_ACCESS_KEY"], region_name="auto")
    c.delete_object(Bucket=e["R2_BUCKET"], Key=key)
    return True


PUB = {"youtube": yt_publish, "facebook": fb_publish, "instagram": ig_publish}


def main():
    q = json.load(open(Q, encoding="utf-8")) if os.path.exists(Q) else []
    t = now()
    fail = 0
    for it in q:
        if it.get("status") != "대기" or it.get("platform") not in PUB:
            continue
        if dt.datetime.fromisoformat(it["publish_at"]) > t:
            continue
        try:
            res = PUB[it["platform"]](it)
            it.update({"status": "게시", "posted_at": t.isoformat(timespec="seconds"), **res})
            log(ep=it["ep"], lang=it["lang"], platform=it["platform"], ok=True, **res)
        except Exception as e:
            it["attempts"] = it.get("attempts", 0) + 1
            it["error"] = str(e)[:300]
            if it["attempts"] >= 3:
                it["status"] = "실패"
                fail += 1
            log(ep=it["ep"], lang=it["lang"], platform=it["platform"], ok=False, error=it["error"], attempts=it["attempts"])
    # R2 정리: 같은 r2_key 의 건이 전부 게시/실패이고 마지막 게시 24h 지남
    keys = {}
    for it in q:
        if it.get("r2_key"):
            keys.setdefault(it["r2_key"], []).append(it)
    for k, items in keys.items():
        if any(x.get("r2_deleted") for x in items):
            continue
        if all(x["status"] in ("게시", "실패") for x in items):
            last = max(dt.datetime.fromisoformat(x.get("posted_at") or x["publish_at"]) for x in items)
            if (t - last).total_seconds() > CLEANUP_H * 3600 and r2_delete(k):
                for x in items:
                    x["r2_deleted"] = t.isoformat(timespec="seconds")
                log(r2_key=k, r2_deleted=True)
    json.dump(q, open(Q, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
