# -*- coding: utf-8 -*-
"""예약표(예약표.json) 깃 합치기 규칙 — 사주 쇼츠저장소/작업/예약표합치기.py 와 같은 파일(복사본) — git merge driver (2026-09-22 사고 전수조사 뒤 추가).

왜 있나: PC(등록)와 깃허브 액션(게시 결과)이 같은 예약표를 고친다. 둘이 엇갈리면 git 이 글자 단위로 합치다 충돌을 내고,
  액션 쪽 "결과 커밋"이 3번 실패하면 게시됐다는 표시가 사라져 다음 회차에 **같은 영상이 또 올라간다**
  (랭킹송 2026-09-20 ko 유튜브 2번 올라간 사고가 이것). PC 쪽은 pull 이 막혀 옛 예약표 위에 등록하다 푸시가 깨진다.
무엇을 하나: 예약표를 글자가 아니라 **건(id) 단위**로 합친다.
  - 한쪽만 고친 건 → 그쪽 것.  한쪽만 새로 넣은 건 → 넣는다.  한쪽이 지웠고 다른 쪽은 안 건드린 건 → 지운다.
  - 양쪽이 같은 건을 고쳤으면 칸(필드)마다: 한쪽만 바꾼 칸은 그쪽, 둘 다 바꾼 칸은 상태 칸이면 "더 나아간 상태"
    (게시·공개·예약·실패·보류 > 대기·없음), 그 외 칸은 지금 얹는 쪽(%B).  → 게시된 표시는 절대 안 사라진다.
어떻게 쓰나 (git 이 부른다. 사람이 직접 칠 일 없음):
  .gitattributes  :  쇼츠예약.json merge=queue
  git config merge.queue.driver "python 작업/예약표합치기.py %O %A %B"   ← PC 는 upload_instagram.git_setup() 이, 액션은 워크플로가 넣는다
  %O = 공통 조상, %A = 현재 쪽(결과를 여기 쓴다), %B = 얹는 쪽. 종료코드 0 = 합쳐짐, 1 = 못 합침(git 이 보통 충돌로 처리).
드라이버 설정이 없으면 git 은 그냥 글자 단위 합치기로 돌아간다(2026-09-22 확인) — 켜져 있지 않아도 지금보다 나빠지진 않는다.
시험: 유튜브업로더/tests/test_merge.py (진짜 git 저장소를 만들어 돌린다).
"""
import io
import sys
import json

STATUS_RANK = {"": 0, None: 0, "없음": 0, "대기": 0, "보류": 1, "예약": 2, "실패": 3, "게시": 3, "공개": 3}


def key_of(it):
    """건의 열쇠. 사주 예약표는 id, 랭킹송 예약표는 (ep, lang, platform)."""
    if isinstance(it, dict):
        if it.get("id") is not None:
            return ("id", it["id"])
        if it.get("ep") is not None:
            return ("epl", it.get("ep"), it.get("lang"), it.get("platform"))
    return ("raw", json.dumps(it, ensure_ascii=False, sort_keys=True))


def _is_status_key(k):
    return k == "status" or k.endswith("_status")


def _rank(v):
    return STATUS_RANK.get(v, 1)      # 모르는 상태값은 대기(0)보다는 위, 게시(3)보다는 아래


def merge_item(base, ours, theirs):
    """같은 열쇠의 건 셋을 칸 단위로 합친다. base 가 None 이면 양쪽이 따로 새로 넣은 것."""
    if ours == theirs:
        return ours
    if base is not None:
        if ours == base:
            return theirs
        if theirs == base:
            return ours
    base = base or {}
    out = {}
    for k in list(ours.keys()) + [k for k in theirs.keys() if k not in ours]:
        o, t, b = ours.get(k, None), theirs.get(k, None), base.get(k, None)
        o_in, t_in = k in ours, k in theirs
        if o == t:
            v, keep = o, (o_in or t_in)
        elif o == b and t_in:            # 우리는 안 건드렸고 저쪽이 바꿈(지운 것 포함)
            v, keep = t, True
        elif o == b and not t_in:         # 저쪽이 칸을 지움
            continue
        elif t == b and o_in:             # 저쪽은 안 건드렸고 우리가 바꿈
            v, keep = o, True
        elif t == b and not o_in:
            continue
        else:                             # 둘 다 바꿈
            if _is_status_key(k):
                v = o if _rank(o) > _rank(t) else t       # 더 나아간 상태. 같으면 얹는 쪽(theirs)
            else:
                v = t
            keep = True
        if keep:
            out[k] = v
    return out


def merge_lists(base, ours, theirs):
    """세 목록을 열쇠 단위로 합친 목록. 순서: base 순 → ours 에만 새로 생긴 것 → theirs 에만 새로 생긴 것."""
    B = {key_of(x): x for x in base}
    O = {key_of(x): x for x in ours}
    T = {key_of(x): x for x in theirs}
    out, seen = [], set()

    def emit(k, v):
        if v is not None and k not in seen:
            out.append(v); seen.add(k)

    for x in base:
        k = key_of(x)
        b, o, t = B[k], O.get(k), T.get(k)
        if o is None and t is None:
            continue                                   # 양쪽 다 지움
        if o is None:                                  # 우리가 지움
            emit(k, None if t == b else t)             # 저쪽이 고쳤으면 살린다
            continue
        if t is None:                                  # 저쪽이 지움
            emit(k, None if o == b else o)
            continue
        emit(k, merge_item(b, o, t))
    for x in ours:
        k = key_of(x)
        if k in B:
            continue
        emit(k, merge_item(None, x, T[k]) if k in T else x)
    for x in theirs:
        k = key_of(x)
        if k in B or k in seen:
            continue
        emit(k, x)
    return out


def _load(path):
    with io.open(path, "r", encoding="utf-8-sig") as f:
        txt = f.read()
    if not txt.strip():
        return []
    return json.loads(txt)


def main(argv):
    if len(argv) != 4:
        sys.stderr.write("usage: 예약표합치기.py BASE OURS THEIRS  (git merge driver: %O %A %B)\n")
        return 2
    base_p, ours_p, theirs_p = argv[1:4]
    try:
        base, ours, theirs = _load(base_p), _load(ours_p), _load(theirs_p)
        if not all(isinstance(x, list) for x in (base, ours, theirs)):
            raise ValueError("예약표는 목록(list)이어야 함")
        merged = merge_lists(base, ours, theirs)
    except Exception as e:
        sys.stderr.write("예약표 합치기 실패(글자 단위 충돌로 넘김): %s\n" % e)
        return 1
    with io.open(ours_p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(merged, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
